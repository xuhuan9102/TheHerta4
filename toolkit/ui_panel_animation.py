# -*- coding: utf-8 -*-

import bpy


class ATP_PT_MainPanel(bpy.types.Panel):
    bl_label = "动画处理工具"
    bl_idname = "VIEW3D_PT_Herta_ATP_Main_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        layout.label(text="版本: 1.7.0")


class ATP_PT_ShapeKeyTools(bpy.types.Panel):
    bl_label = "形态键统一控制器"
    bl_idname = "VIEW3D_PT_Herta_ATP_ShapeKeyTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        from . import at_shape_key_control
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        box.label(text="统一控制器:")
        for item in props.shape_key_list:
            row = box.row(align=True)
            split = row.split(factor=0.4)
            split.label(text=item.name)
            split.prop(item, "value", text="", slider=True)


class ATP_PT_ShapeKeyOperations(bpy.types.Panel):
    bl_label = "形态键操作"
    bl_idname = "VIEW3D_PT_Herta_ATP_ShapeKeyOperations_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_ShapeKeyTools_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        from . import at_shape_key_control
        from . import at_shape_key_operations
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        
        col = box.column(align=True)
        
        col.label(text="复制功能:", icon='COPYDOWN')
        row = col.row(align=True)
        row.operator(at_shape_key_control.ATP_OT_RefreshShapeKeys.bl_idname, text="刷新列表", icon='FILE_REFRESH')
        row.operator(at_shape_key_control.ATP_OT_CopyShapeKeys.bl_idname, text="复制到选中项", icon='COPYDOWN')

        col.separator()
        col.label(text="复制形态键旋转校正:", icon='ORIENTATION_LOCAL')
        col.prop(props, "copy_sk_use_manual_rotation")
        if props.copy_sk_use_manual_rotation:
            col.prop(props, "copy_sk_rotation_x")
            col.prop(props, "copy_sk_rotation_y")
            col.prop(props, "copy_sk_rotation_z")

        col.separator()
        col.label(text="批量处理 (按名称):", icon='ADD')
        col.prop(props, "sk_add_new_name", text="")
        row = col.row(align=True)
        row.operator(at_shape_key_operations.ATP_OT_BatchAddShapeKey.bl_idname, text="添加", icon='ADD')
        row.operator(at_shape_key_operations.ATP_OT_BatchRemoveShapeKey.bl_idname, text="删除", icon='REMOVE')

        col.separator()
        col.label(text="批量控制:", icon='KEY_DEHLT')
        row = col.row(align=True)
        row.operator(at_shape_key_operations.ATP_OT_ResetAllShapeKeys.bl_idname, text="归零所有形态键", icon='KEY_DEHLT')
        
        col.separator()
        col.label(text="设置活动形态键:", icon='SHAPEKEY_DATA')
        col.prop(props, "sk_set_active_name", text="")
        row = col.row(align=True)
        row.operator(at_shape_key_operations.ATP_OT_SetActiveShapeKey.bl_idname, text="设为活动", icon='SHAPEKEY_DATA')
        
        col.separator()
        col.label(text="批量重命名形态键:", icon='OUTLINER_OB_GROUP_INSTANCE')
        row = col.row(align=True)
        col.prop(props, "sk_rename_old_name", text="原名称")
        col.prop(props, "sk_rename_new_name", text="新名称")
        row = col.row(align=True)
        row.operator(at_shape_key_operations.ATP_OT_BatchRenameShapeKey.bl_idname, text="重命名", icon='OUTLINER_OB_GROUP_INSTANCE')


class ATP_PT_ShapeKeyCreation(bpy.types.Panel):
    bl_label = "形状差异与帧拆分"
    bl_idname = "VIEW3D_PT_Herta_ATP_ShapeKeyCreation_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_ShapeKeyTools_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        from . import at_shape_key_creation
        from . import at_multi_frame_split
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        
        col = box.column(align=True)
        
        col.label(text="形状差异转形态键:", icon='MOD_MESHDEFORM')
        col.prop(props, "shape_diff_key_name", text="形态键名称")
        col.operator(at_shape_key_creation.ATP_OT_ObjectToShapeKey.bl_idname, text="将形状差异转换为形态键", icon='MOD_MESHDEFORM')
        col.label(text="提示: 选择两个物体，第一个为基础，第二个为目标", icon='QUESTION')

        col.separator()
        col.label(text="多物体拆分帧到形态键（增强版）:", icon='MOD_MESHDEFORM')
        
        row = col.row()
        row.label(text="帧/形态键对列表:")
        row.operator(at_shape_key_creation.ATP_OT_AddFrameShapeKeyPair.bl_idname, text="", icon='ADD')
        row.operator(at_shape_key_creation.ATP_OT_AddDefaultFrameShapeKeyPairs.bl_idname, text="", icon='PRESET')
        row.operator(at_shape_key_creation.ATP_OT_ClearFrameShapeKeyPairs.bl_idname, text="", icon='X')
        
        if props.frame_shape_key_pairs:
            col2 = col.column(align=True)
            for i, pair in enumerate(props.frame_shape_key_pairs):
                row = col2.row(align=True)
                row.prop(pair, "end_frame", text=f"帧")
                row.prop(pair, "shape_key_name", text="")
                row.prop(pair, "is_processed", text="")
                op = row.operator(at_shape_key_creation.ATP_OT_RemoveFrameShapeKeyPair.bl_idname, text="", icon='REMOVE')
                op.index = i
        else:
            col.label(text="暂无帧/形态键对", icon='INFO')
        
        col.prop(props, "multi_object_start_frame", text="起始帧")
        row = col.row(align=True)
        row.prop(props, "use_precise_frame_mode", text="高精度模式")
        row.prop(props, "use_continuous_mode", text="连续模式")
        
        if props.show_processing_status:
            col.prop(props, "show_processing_status", text="显示处理状态", icon='HIDE_OFF')
            
            if props.current_processing_status:
                col.label(text=props.current_processing_status, icon='TIME')
            
            if props.current_processing_progress > 0:
                progress_text = f"进度: {props.current_processing_progress*100:.1f}%"
                box.progress(factor=props.current_processing_progress, text=progress_text)
            
            total_pairs = len(props.frame_shape_key_pairs)
            processed_pairs = sum(1 for pair in props.frame_shape_key_pairs if pair.is_processed)
            if total_pairs > 0:
                col.label(text=f"已完成: {processed_pairs}/{total_pairs} 帧/形态键对", icon='CHECKMARK')
            
        else:
            col.prop(props, "show_processing_status", text="显示处理状态", icon='HIDE_ON')
        
        row = col.row(align=True)
        row.operator(at_multi_frame_split.ATP_OT_SplitFramesToShapeKeyMulti.bl_idname, text="处理多物体", icon='PLAY')
        row.operator(at_shape_key_creation.ATP_OT_StopMultiProcessing.bl_idname, text="停止", icon='PAUSE')
        
        selected_mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if selected_mesh_objects:
            col.label(text=f"已选择 {len(selected_mesh_objects)} 个网格物体", icon='INFO')
        else:
            col.label(text="请先选择网格物体", icon='ERROR')
        
        col.label(text="提示: 每个物体将按顺序处理所有帧/形态键对", icon='QUESTION')


class ATP_PT_ShapeKeyAnimationExport(bpy.types.Panel):
    bl_label = "形态键动画序列导出"
    bl_idname = "VIEW3D_PT_Herta_ATP_ShapeKeyAnimationExport_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_ShapeKeyTools_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    def draw(self, context):
        from . import at_shape_key_creation
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        
        col = box.column(align=True)
        
        col.label(text="导出所选物体的形态键动画序列:", icon='SHAPEKEY_DATA')
        col.label(text="生成播放表并输出到文本编辑器", icon='TEXT')
        
        col.separator()
        
        row = col.row(align=True)
        row.prop(props, "shape_anim_export_start_frame", text="起始帧")
        row.prop(props, "shape_anim_export_end_frame", text="结束帧")
        
        col.separator()
        
        col.label(text="播放表格式示例:", icon='INFO')
        example_box = col.box()
        example_box.label(text="Key_发 = 0.5,0.25,1,0.68,0.6,0.7")
        example_box.label(text="Key_脸 = 0.0,0.3,0.5,0.8,1.0,0.5")
        
        col.separator()
        
        selected_mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if selected_mesh_objects:
            col.label(text=f"已选择 {len(selected_mesh_objects)} 个网格物体", icon='INFO')
        else:
            col.label(text="请先选择网格物体", icon='ERROR')
        
        col.separator()
        
        col.operator(at_shape_key_creation.ATP_OT_ShapeKeyAnimationExport.bl_idname, 
                    icon='PLAY', text="导出形态键动画序列")


class ATP_PT_AlembicTools(bpy.types.Panel):
    bl_label = "烘焙与自动导入 (Alembic)"
    bl_idname = "VIEW3D_PT_Herta_ATP_AlembicTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        from . import at_alembic_tools
        
        layout = self.layout
        props = context.scene.atp_props

        box = layout.box()
        box.label(text="设置与操作", icon='EXPORT')

        col = box.column(align=True)
        col.prop(props, "abc_export_folder")
        col.prop(props, "abc_import_collection")

        row = box.row(align=True)
        row.prop(props, "abc_export_scale")
        row.prop(props, "abc_export_uvs")

        col.separator()
        col.prop(props, "abc_transfer_vertex_groups")
        if props.abc_transfer_vertex_groups and len(context.selected_objects) != 1:
            col.label(text="提示: 权重传递仅在单选物体时生效", icon='INFO')

        box.separator()
        if context.active_object:
            box.label(text=f"输出: .../{props.abc_export_folder}/{context.active_object.name}.abc", icon='FILE_FOLDER')
        box.label(text=f"帧范围: {context.scene.frame_start} - {context.scene.frame_end}", icon='TIME')

        enabled = True
        if not bpy.data.is_saved:
            box.label(text="错误: 请先保存 .blend 文件", icon='ERROR')
            enabled = False
        elif not context.selected_objects:
            box.label(text="错误: 请选择要烘焙的物体", icon='ERROR')
            enabled = False
            
        col = box.column()
        col.enabled = enabled
        col.operator(at_alembic_tools.ATP_OT_BakeAndImportAlembic.bl_idname, icon='PLAY', text="执行烘焙与导入")


class ATP_PT_AnimationFrameSplit(bpy.types.Panel):
    bl_label = "动画帧拆分 (独立)"
    bl_idname = "VIEW3D_PT_Herta_ATP_AnimationFrameSplit_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    def draw(self, context):
        from . import at_alembic_tools
        from . import at_utils
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        box.label(text="将选中物体的每一帧拆分为新物体", icon='SEQUENCE')
        box.label(text="并在源集合下创建子集合存放", icon='OUTLINER_COLLECTION')
        
        box.separator()
        box.prop(props, "be_object_prefix", text="新物体名称前缀")
        box.prop(props, "anim_split_sub_prefix")
        
        row = box.row(align=True)
        row.prop(props, "anim_split_start_frame")
        row.prop(props, "anim_split_end_frame")
        box.prop(props, "anim_split_playback_type")

        has_selection = bool(context.selected_objects)
        has_alembic = any(at_utils.is_alembic_object(obj) for obj in context.selected_objects)
        
        col = box.column()
        col.enabled = not has_alembic 
        col.prop(props, "anim_split_separate_static")
        
        if has_alembic:
            box.label(text="提示: Alembic物体不支持静态分离", icon='INFO')
        elif props.anim_split_separate_static:
            box.prop(props, "anim_split_static_tolerance")

        box.separator()
        box.prop(props, "anim_split_set_linear")
        
        op_row = box.row()
        op_row.enabled = has_selection
        op_row.operator(at_alembic_tools.ATP_OT_SplitAnimation.bl_idname, icon='PLAY', text="执行拆分")


class ATP_PT_Automation(bpy.types.Panel):
    bl_label = "自动化(依赖ssmt)"
    bl_idname = "VIEW3D_PT_Herta_ATP_Automation_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        props = context.scene.atp_props

        box = layout.box()
        box.label(text="共享设置:", icon='SETTINGS')
        
        col = box.column(align=True)
        col.prop(props, "auto_export_base_path")
        
        col.separator()
        col.label(text="执行延时 (用于观察/IO等待):", icon='TIME')
        row = col.row(align=True)
        row.prop(props, "auto_step_delay_seconds")
        row.prop(props, "auto_final_delay_seconds")


class ATP_PT_AutomationShapeKeyExport(bpy.types.Panel):
    bl_label = "自动化形态键导出"
    bl_idname = "VIEW3D_PT_Herta_ATP_AutomationShapeKeyExport_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Automation_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        from . import at_shape_key_export
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        
        col = box.column(align=True)
        
        col.separator()
        col.label(text="SSMT导出设置:", icon='SETTINGS')
        col.prop(props, "ske_node_tree_name")
        
        col.separator()
        if props.show_ske_instructions:
            col.prop(props, "show_ske_instructions", text="隐藏流程说明", icon='TRIA_DOWN')
            col.label(text="流程说明:", icon='INFO')
            box_flow = col.box()
            box_flow.label(text="1. 从节点树中收集所有物体")
            box_flow.label(text="2. 分类所有形态键并输出报告到文本编辑器")
            box_flow.label(text="3. 导出基础形态 (Buffer0000)")
            box_flow.label(text="4. 按槽位顺序导出各变形形态 (Buffer1001 ...)")
            box_flow.label(text="5. 保留所有导出文件 (.ini及Buffer内容)")
            box_flow.label(text="6. 结束后将所有形态键值归零")
        else:
            col.prop(props, "show_ske_instructions", text="显示流程说明", icon='TRIA_RIGHT')
        
        box.separator()
        box.label(text="点击后请留意弹出的系统控制台", icon='INFO')
        box.operator(at_shape_key_export.ATP_OT_ShapeKeyExport.bl_idname, icon='PLAY', text="启动自动化形态键导出")


class ATP_PT_AutomationBufferMerge(bpy.types.Panel):
    bl_label = "动画导出，顶点缓冲合并"
    bl_idname = "VIEW3D_PT_Herta_ATP_AutomationBufferMerge_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_ATP_Automation_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        from . import at_animation_export
        from . import at_buffer_merge
        
        layout = self.layout
        props = context.scene.atp_props
        
        box = layout.box()
        
        col = box.column(align=True)

        col.label(text="动画导出（ZZZ）:", icon='SEQUENCE')
        col.prop(props, "export_armature")
        col.prop(props, "export_mesh")
        col.prop(props, "export_filepath")
        row = col.row(align=True)
        row.prop(props, "export_frame_start")
        row.prop(props, "export_frame_end")
        col.prop(props, "export_floor_offset")
        col.operator(at_animation_export.BMTP_OT_ExportAnimation.bl_idname, icon='PLAY', text="执行导出")

        box.separator()

        col = box.column(align=True)

        col.label(text="顶点缓冲合并 (.buf):", icon='FILE_SCRIPT')
        col.prop(props, "merge_buf_path")
        
        row = col.row()
        row.scale_y = 1.5
        row.enabled = bool(props.merge_buf_path)
        row.operator(at_buffer_merge.BMTP_OT_MergeBuffers.bl_idname, text="执行合并", icon='PLAY')


ui_panel_animation_list = (
    ATP_PT_MainPanel,
    ATP_PT_ShapeKeyTools,
    ATP_PT_AlembicTools,
    ATP_PT_AnimationFrameSplit,
    ATP_PT_Automation,
    ATP_PT_ShapeKeyOperations,
    ATP_PT_ShapeKeyCreation,
    ATP_PT_ShapeKeyAnimationExport,
    ATP_PT_AutomationShapeKeyExport,
    ATP_PT_AutomationBufferMerge,
)
