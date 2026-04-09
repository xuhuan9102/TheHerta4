# -*- coding: utf-8 -*-

import bpy


def update_shape_key_value(self, context):
    """当UI滑块变动时，更新所有选中物体上对应的形态键值"""
    shape_key_name = self.name
    new_value = self.value
    
    for obj in context.selected_objects:
        if obj.type == 'MESH' and obj.data.shape_keys:
            if shape_key_name in obj.data.shape_keys.key_blocks:
                obj.data.shape_keys.key_blocks[shape_key_name].value = new_value
                obj.data.update_tag()


class ATP_ShapeKeyItem(bpy.types.PropertyGroup):
    """用于在UI列表中存储一个形态键信息的属性组"""
    name: bpy.props.StringProperty()
    value: bpy.props.FloatProperty(
        name="Value",
        min=0.0,
        soft_min=0.0,
        default=0.0,
        update=update_shape_key_value,
        description="统一控制所有同名形态键的值"
    )


class ATP_FrameShapeKeyPair(bpy.types.PropertyGroup):
    """用于存储帧号和对应的形态键名称的属性组"""
    end_frame: bpy.props.IntProperty(
        name="结束帧",
        default=10,
        min=1,
        description="该形态键对应的结束帧"
    )
    shape_key_name: bpy.props.StringProperty(
        name="形态键名称",
        default="Motion_Key",
        description="该帧对应的形态键名称"
    )
    is_processed: bpy.props.BoolProperty(
        name="已处理",
        default=False,
        description="标记该帧/形态键对是否已处理完成"
    )


class ATP_UL_FrameShapeKeyList(bpy.types.UIList):
    """帧/形态键对的UI列表"""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            
            if item.is_processed:
                row.label(icon='CHECKMARK', text="")
            else:
                row.label(icon='DOT', text="")
            
            row.prop(item, "end_frame", text="帧", emboss=False)
            row.prop(item, "shape_key_name", text="", emboss=False)
            
            op = row.operator("atp.remove_frame_shape_key_pair", text="", icon='X')
            op.index = index
            
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=f"{item.end_frame}: {item.shape_key_name}")


class ATP_Properties(bpy.types.PropertyGroup):
    """插件的全局属性"""
    
    shape_key_list: bpy.props.CollectionProperty(type=ATP_ShapeKeyItem, name="形态键列表")
    shape_key_list_index: bpy.props.IntProperty(name="形态键列表索引", default=0)
    sk_add_new_name: bpy.props.StringProperty(name="形态键名称", default="NewKey", description="要批量添加或删除的形态键的名称")
    sk_set_active_name: bpy.props.StringProperty(name="活动形态键名称", default="", description="要设置为活动项的形态键名称")
    sk_rename_old_name: bpy.props.StringProperty(name="原形态键名称", default="", description="要重命名的形态键的原名称")
    sk_rename_new_name: bpy.props.StringProperty(name="新形态键名称", default="", description="形态键的新名称")

    anim_split_sub_prefix: bpy.props.StringProperty(name="子合集前缀", default="Frame_")
    anim_split_start_frame: bpy.props.IntProperty(name="起始帧", default=1, min=0)
    anim_split_end_frame: bpy.props.IntProperty(name="结束帧", default=30, min=0)
    anim_split_playback_type: bpy.props.EnumProperty(
        name="播放模式",
        items=[('PRECISE', "精确", "从第1帧播放到目标帧，确保物理和依赖的正确性"),
               ('SIMPLE', "简单", "直接跳转到目标帧，速度快")],
        default='PRECISE'
    )
    anim_split_separate_static: bpy.props.BoolProperty(name="分离静态顶点", default=False,
                                                       description="将动画中不变的顶点分离成一个单独的基础物体，以优化性能")
    anim_split_static_tolerance: bpy.props.FloatProperty(name="静态容差", default=0.001, min=0.0,
                                                         description="判断顶点是否为静态的位置变化容差")
    anim_split_set_linear: bpy.props.BoolProperty(name="设为线性插值", default=False,
                                                  description="在拆分前，将选中对象所有关键帧的插值模式设为线性")

    be_start_frame: bpy.props.IntProperty(name="起始帧", default=1, min=1, description="自动化流程从指定的帧数开始执行")
    be_loop_count: bpy.props.IntProperty(name="结束帧", default=15, min=1, description="自动化流程执行的总次数 (作为结束帧)")
    
    be_ini_file_prefix: bpy.props.StringProperty(name="INI前缀", default="filename",
                                                 description="重命名后的.ini文件的前缀")
    be_object_prefix: bpy.props.StringProperty(name="物体前缀", default="",
                                               description="拆分动画帧后创建的新物体的前缀")

    be_playback_type: bpy.props.EnumProperty(
        name="播放模式",
        items=[('PRECISE', "精确", "从第1帧播放到目标帧，确保物理和依赖的正确性"),
               ('SIMPLE', "简单", "直接跳转到目标帧，速度快")],
        default='PRECISE', description="拆分动画时跳转到目标帧的方式"
    )

    auto_export_base_path: bpy.props.StringProperty(name="导出路径", default="", subtype='DIR_PATH',
                                                  description="导出文件和文件夹存放的基础路径")
    auto_top_level_collection_name: bpy.props.StringProperty(name="顶层集合名称", default="Export",
                                                             description="自动化流程使用的顶层/导出集合名称")
    auto_step_delay_seconds: bpy.props.FloatProperty(name="步骤延时(秒)", default=0.2, min=0.0,
                                                   description="自动化流程中每一步之间的暂停时间，便于观察")
    auto_final_delay_seconds: bpy.props.FloatProperty(name="结束延时(秒)", default=5.0, min=0.0,
                                                    description="整个流程结束后，关闭控制台前的等待时间")

    show_be_instructions: bpy.props.BoolProperty(name="显示动画导出流程说明", default=False,
                                            description="是否显示自动化动画导出的流程说明")
    show_ske_instructions: bpy.props.BoolProperty(name="显示形态键导出流程说明", default=False,
                                             description="是否显示自动化形态键导出的流程说明")
    ske_node_tree_name: bpy.props.StringProperty(name="节点树名称", default="Mod_爱芮",
                                                 description="SSMT导出时使用的节点树名称")

    abc_export_folder: bpy.props.StringProperty(name="导出子文件夹", default="Alembic_Exports",
                                                description="存放.abc文件的文件夹，将创建在.blend文件所在目录下")
    abc_import_collection: bpy.props.StringProperty(name="导入到集合", default="Alembic_Imports",
                                                    description="自动导入的.abc物体将被放置到此集合中")
    abc_export_scale: bpy.props.FloatProperty(name="缩放", default=1.0, min=0.01, description="导出Alembic时的全局缩放")
    abc_export_uvs: bpy.props.BoolProperty(name="导出UV", default=True, description="是否在Alembic文件中包含UV信息")
    abc_transfer_vertex_groups: bpy.props.BoolProperty(name="传递顶点组", default=True,
                                                       description="将原物体的顶点组和权重传递给烘焙后的物体（仅限单选物体）")

    shape_diff_key_name: bpy.props.StringProperty(name="形态键名称", default="", 
                                                 description="新形态键的名称（留空则使用目标物体名称）")

    copy_sk_rotation_x: bpy.props.FloatProperty(name="X轴旋转(度)", default=0.0,
                                                 description="目标物体相对于源物体在X轴的旋转角度（度）")
    copy_sk_rotation_y: bpy.props.FloatProperty(name="Y轴旋转(度)", default=0.0,
                                                 description="目标物体相对于源物体在Y轴的旋转角度（度）")
    copy_sk_rotation_z: bpy.props.FloatProperty(name="Z轴旋转(度)", default=0.0,
                                                 description="目标物体相对于源物体在Z轴的旋转角度（度）")
    copy_sk_use_manual_rotation: bpy.props.BoolProperty(name="使用手动旋转", default=False,
                                                       description="是否使用手动输入的旋转角度来校正形态键")
    
    frame_shape_key_pairs: bpy.props.CollectionProperty(
        type=ATP_FrameShapeKeyPair,
        name="帧/形态键对列表",
        description="配置多个结束帧和对应的形态键名称"
    )
    frame_shape_key_index: bpy.props.IntProperty(
        name="当前选中索引",
        default=0,
        min=0
    )
    multi_object_start_frame: bpy.props.IntProperty(
        name="起始帧",
        default=1,
        min=1,
        description="所有物体的起始帧"
    )
    use_precise_frame_mode: bpy.props.BoolProperty(
        name="高精度模式",
        default=True,
        description="高精度模式：逐帧播放到目标帧（精确但慢）\n简单模式：直接跳转到目标帧（快速但可能不够精确）"
    )
    use_continuous_mode: bpy.props.BoolProperty(
        name="连续模式",
        default=False,
        description="连续模式：以上一个形态键帧为基准进行连续计算（如1→5→10）\n独立模式：每个形态键帧均以起始帧为基准进行独立计算（如1→5、1→10）"
    )
    show_processing_status: bpy.props.BoolProperty(
        name="显示处理状态",
        default=True,
        description="在UI中显示处理状态信息"
    )
    stop_processing_flag: bpy.props.BoolProperty(
        name="停止处理标志",
        default=False,
        description="用于停止正在运行的处理"
    )
    current_processing_status: bpy.props.StringProperty(
        name="当前处理状态",
        default="",
        description="当前多物体处理的状态信息"
    )
    current_processing_progress: bpy.props.FloatProperty(
        name="当前处理进度",
        default=0.0,
        min=0.0,
        max=1.0,
        description="当前多物体处理的进度 (0.0-1.0)"
    )

    export_armature: bpy.props.PointerProperty(
        type=bpy.types.Object, name="骨架", poll=lambda self, obj: obj.type == 'ARMATURE'
    )
    export_mesh: bpy.props.PointerProperty(
        type=bpy.types.Object, name="网格", poll=lambda self, obj: obj.type == 'MESH'
    )
    export_filepath: bpy.props.StringProperty(name="输出路径", subtype='FILE_PATH', default="//pose.buf")
    export_frame_start: bpy.props.IntProperty(name="开始帧", default=1, min=0)
    export_frame_end: bpy.props.IntProperty(name="结束帧", default=250, min=0)
    export_floor_offset: bpy.props.FloatProperty(name="地面偏移", default=0.0)

    merge_buf_path: bpy.props.StringProperty(
        name="目标路径",
        description="选择包含-Position.buf和-Texcoord.buf文件的文件夹",
        subtype='DIR_PATH',
        default="//"
    )

    shape_anim_export_start_frame: bpy.props.IntProperty(
        name="起始帧",
        default=1,
        min=1,
        description="形态键动画序列导出的起始帧"
    )
    shape_anim_export_end_frame: bpy.props.IntProperty(
        name="结束帧",
        default=30,
        min=1,
        description="形态键动画序列导出的结束帧"
    )

    slider_check_hash: bpy.props.StringProperty(
        name="检测Hash值",
        default="",
        description="用于检测当前角色的hash值 (如: 8b240678)，留空则不生成hash行"
    )

    slider_match_index_count: bpy.props.IntProperty(
        name="Match Index Count",
        default=0,
        min=0,
        description="匹配索引数量 (如: 554564)，设为0则不生成match_index_count行"
    )


at_properties_list = (
    ATP_ShapeKeyItem,
    ATP_FrameShapeKeyPair,
    ATP_UL_FrameShapeKeyList,
    ATP_Properties,
)
