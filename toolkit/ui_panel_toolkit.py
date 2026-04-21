import bpy


class ToolkitPanel(bpy.types.Panel):
    bl_label = "工具集"
    bl_idname = "VIEW3D_PT_Herta_Toolkit_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_order = 1

    @classmethod
    def poll(cls, context):
        if not hasattr(context.scene, 'herta_show_toolkit'):
            return False
        return context.scene.herta_show_toolkit

    def draw(self, context):
        layout = self.layout
        
        row = layout.row()
        row.prop(context.scene, 'herta_show_toolkit', text="工具集模式")
        row.operator("model.switch_to_main_panel", text="", icon='BACK')


class VGToolsPanel(bpy.types.Panel):
    bl_label = "顶点组处理工具"
    bl_idname = "VIEW3D_PT_Herta_VGTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        self._draw_vertex_group_rename(layout, context)
        self._draw_vertex_group_create(layout, context)
        self._draw_vertex_group_numeric(layout, context)

    def _draw_vertex_group_rename(self, layout, context):
        box = layout.box()
        box.label(text="顶点组重命名与骨骼", icon='BONE_DATA')
        box.operator("toolkit.rename_vertex_group_name_with_their_suffix")
        box.operator("toolkit.add_bone_from_vertex_group_v2")

    def _draw_vertex_group_create(self, layout, context):
        props = context.scene.vg_props
        
        box = layout.box()
        box.label(text="顶点组创建与清理", icon='ADD')
        
        sub_box = box.box()
        sub_box.label(text="创建组:")
        sub_box.prop(props, "vg_create_full_name")
        sub_box.prop(props, "vg_create_empty_name")
        sub_box.prop(props, "vg_create_delete_existing")
        sub_box.operator("toolkit.create_vgs_and_uv", icon='PLUS', text="创建组和UV")
        
        sub_box = box.box()
        sub_box.label(text="批量重命名:")
        row = sub_box.row()
        row.prop(props, "vg_rename_old_name", text="")
        row.prop(props, "vg_rename_new_name", text="")
        sub_box.operator("toolkit.batch_rename_vg", icon='SORTALPHA')
        
        sub_box = box.box()
        sub_box.label(text="批量删除:")
        sub_box.prop(props, "vg_cleanup_names")
        sub_box.operator("toolkit.batch_delete_vg", icon='REMOVE')
        
        sub_box = box.box()
        sub_box.label(text="清理工具:")
        sub_box.prop(props, "vg_cleanup_remove_zero")
        sub_box.operator("toolkit.clean_vertex_groups", text="执行清理")

    def _draw_vertex_group_numeric(self, layout, context):
        props = context.scene.vg_props

        box = layout.box()
        box.label(text="数字格式化工具", icon='LINENUMBERS_ON')

        sub_box = box.box()
        row = sub_box.row()
        row.prop(props, "vg_merge_sync_bones")
        row.operator("toolkit.merge_vg_by_prefix", icon='AUTOMERGE_ON', text="按数字前缀合并")

        sub_box = box.box()
        sub_box.operator("toolkit.remove_non_numeric_vg", icon='REMOVE')
        sub_box.operator("toolkit.fill_vg_number_gaps", icon='LINENUMBERS_ON')


class BMTP_MainPanel(bpy.types.Panel):
    bl_label = "骨骼与模型处理工具"
    bl_idname = "VIEW3D_PT_Herta_BMTP_Main_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        pass


class BMTP_BoneControlPanel(bpy.types.Panel):
    bl_label = "骨骼控制"
    bl_idname = "VIEW3D_PT_Herta_BMTP_BoneControl_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="骨骼显示控制", icon='HIDE_OFF')
        col = box.column(align=True)
        col.operator("toolkit.bmtp_show_used_bones", icon='VIEW3D')
        col.operator("toolkit.bmtp_show_all_bones", icon='HIDE_OFF')

        box = layout.box()
        box.label(text="骨骼通用工具 (作用于活动骨骼)", icon='ARMATURE_DATA')
        col = box.column(align=True)
        col.operator("toolkit.bmtp_clean_unused_bones")
        row = col.row(align=True)
        row.prop(props, "align_axis", text="")
        row.operator("toolkit.bmtp_align_bones", text="局部轴向对齐")

        box = layout.box()
        box.label(text="骨骼重命名/恢复", icon='SORTALPHA')
        col = box.column(align=True)
        col.operator("toolkit.bmtp_remap_bones_to_indices")
        col.separator()
        col.prop(props, "restore_map_text", text="")
        restore_row = col.row()
        restore_row.enabled = bool(props.restore_map_text)
        restore_row.operator("toolkit.bmtp_restore_bone_names")


class BMTP_WeightControlPanel(bpy.types.Panel):
    bl_label = "权重控制"
    bl_idname = "VIEW3D_PT_Herta_BMTP_WeightControl_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        pass


class BMTP_WeightOperationPanel(bpy.types.Panel):
    bl_label = "权重操作"
    bl_idname = "VIEW3D_PT_Herta_BMTP_WeightOperation_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_WeightControl_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="权重传递", icon='MOD_SUBSURF')
        box.prop(props, "wt_source_obj")
        
        refresh_row = box.row()
        refresh_row.enabled = props.wt_source_obj is not None
        refresh_row.operator("toolkit.bmtp_refresh_vertex_groups", icon='FILE_REFRESH')
        
        if props.wt_source_obj:
            box.label(text="提示：点击刷新按钮会备份所有顶点组的权重数据", icon='HELP')
        
        box.prop(props, "wt_use_shapekey_positions")
        box.prop(props, "wt_use_armature_positions")
        
        try:
            box.prop(props, "wt_use_selected_groups")
            
            if props.wt_use_selected_groups:
                if props.wt_source_obj and props.wt_source_obj.vertex_groups:
                    box.label(text="只有选中的顶点组会被传递", icon='INFO')
                    
                    box.template_list("BMTP_UL_VertexGroupList", "", props, "wt_vertex_groups", props, "wt_vertex_groups_index", rows=5)
                    
                    row = box.row(align=True)
                    row.operator("toolkit.bmtp_select_all_vertex_groups", text="全选").select = True
                    row.operator("toolkit.bmtp_select_all_vertex_groups", text="全不选").select = False
                else:
                    box.label(text="源物体没有顶点组或未选择源物体", icon='ERROR')
            else:
                box.label(text="将传递所有顶点组", icon='INFO')
        except AttributeError:
            box.label(text="属性未正确加载，请重新加载插件", icon='ERROR')
        
        box.prop(props, "wt_cleanup")
        
        if props.wt_use_selected_groups:
            box.operator("toolkit.bmtp_transfer_weights", text="传递选中的顶点组", icon='PLAY')
        else:
            box.operator("toolkit.bmtp_transfer_weights", text="传递所有顶点组", icon='PLAY')

        box = layout.box()
        box.label(text="权重平滑 (作用于选中物体)", icon='SMOOTHCURVE')
        col = box.column(align=True)
        col.prop(props, "wt_smooth_factor")
        col.prop(props, "wt_smooth_repeat")
        col.separator()
        col.operator("toolkit.bmtp_smooth_weights", icon='PLAY')

        box = layout.box()
        box.label(text="权重扩散 (作用于选中物体)", icon='MOD_SUBSURF')
        col = box.column(align=True)
        col.prop(props, "wt_spread_iterations")
        col.separator()
        col.operator("toolkit.bmtp_spread_weights", icon='PLAY')


class BMTP_WeightManagePanel(bpy.types.Panel):
    bl_label = "权重管理"
    bl_idname = "VIEW3D_PT_Herta_BMTP_WeightManage_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_WeightControl_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        self._draw_vertex_group_backup(layout, context)
        self._draw_weight_adjust(layout, context)

    def _draw_vertex_group_backup(self, layout, context):
        obj = context.active_object
        selected_objects = context.selected_objects
        selected_meshes = [obj for obj in selected_objects if obj.type == 'MESH']
        
        box = layout.box()
        box.label(text="权重备份与恢复", icon='FILE_BACKUP')
        
        if obj and obj.type == 'MESH':
            sub_box = box.box()
            row = sub_box.row()
            row.template_list("VGBackupListUI", "", obj, "vg_backups", obj, "vg_backups_index")

            col = row.column(align=True)
            col.operator("toolkit.backup_vg_weights", text="", icon='ADD')
            col.operator("toolkit.remove_vg_backup", text="", icon='REMOVE')
            
            sub_box.operator("toolkit.restore_vg_weights", icon='RECOVER_LAST', text="恢复选中备份")
        else:
            box.label(text="请选择一个网格物体", icon='ERROR')
        
        box.separator()
        sub_box = box.box()
        sub_box.label(text=f"已选择 {len(selected_meshes)} 个网格物体", icon='MESH_DATA')
        sub_box.operator("toolkit.batch_backup_vg_weights", icon='ADD', text="为所有选中物体创建备份")
        
        sub_box = box.box()
        sub_box.label(text=f"已选择 {len(selected_meshes)} 个网格物体", icon='MESH_DATA')
        sub_box.operator("toolkit.batch_restore_vg_weights", icon='RECOVER_LAST', text="为所有选中物体恢复权重")

    def _draw_weight_adjust(self, layout, context):
        vg_props = context.scene.vg_props
        
        box = layout.box()
        box.label(text="权重调整", icon='MODIFIER')
        
        sub_box = box.box()
        sub_box.label(text="权重调整（勾选顶点组）:", icon='GROUP_VERTEX')
        
        row = sub_box.row()
        row.template_list("VGAdjustListUI", "available", vg_props, "vg_adjust_available_groups", vg_props, "vg_adjust_available_groups_index")
        
        col = row.column(align=True)
        col.operator("toolkit.refresh_vg_list", text="", icon='FILE_REFRESH')
        col.operator("toolkit.select_all_vg", text="", icon='CHECKBOX_HLT')
        col.operator("toolkit.deselect_all_vg", text="", icon='CHECKBOX_DEHLT')
        col.operator("toolkit.invert_vg_selection", text="", icon='ARROW_LEFTRIGHT')
        
        sub_box = box.box()
        sub_box.label(text="规格化（选择顶点组）:", icon='NORMALIZE_FCURVES')
        
        row = sub_box.row()
        row.template_list("VGAdjustListUI", "selected", vg_props, "vg_adjust_selected_groups", vg_props, "vg_adjust_selected_groups_index")
        
        col = row.column(align=True)
        col.operator("toolkit.add_vg_to_adjust_list", text="", icon='ADD')
        col.operator("toolkit.remove_vg_from_adjust_list", text="", icon='REMOVE')
        col.operator("toolkit.clear_vg_adjust_list", text="", icon='X')
        
        sub_box = box.box()
        sub_box.label(text="权重调整参数:", icon='MODIFIER')
        sub_box.prop(vg_props, "vg_adjust_amount")
        sub_box.prop(vg_props, "vg_adjust_mode")
        sub_box.operator("toolkit.adjust_vg_weights", icon='ARROW_LEFTRIGHT', text="调整权重")
        
        sub_box = box.box()
        sub_box.label(text="规格化参数:", icon='NORMALIZE_FCURVES')
        sub_box.prop(vg_props, "vg_normalize_mode")
        sub_box.operator("toolkit.normalize_vg_weights", icon='NORMALIZE_FCURVES', text="规格化权重")


class BMTP_ModelControlPanel(bpy.types.Panel):
    bl_label = "模型控制"
    bl_idname = "VIEW3D_PT_Herta_BMTP_ModelControl_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="设置顶点色", icon='COLOR')
        box.prop(props, "vc_mode", expand=True)
        box.prop(props, "vc_color")
        box.operator("toolkit.bmtp_set_vertex_color", icon='PLAY')


class BMTP_MeshEditPanel(bpy.types.Panel):
    bl_label = "网格编辑"
    bl_idname = "VIEW3D_PT_Herta_BMTP_MeshEdit_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_ModelControl_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="动态桥接工具", icon='MOD_SUBSURF')
        col = box.column(align=True)
        col.prop(props, "bridge_segments")
        col.prop(props, "bridge_reverse")
        col.prop(props, "bridge_smooth")
        col.separator()
        col.operator("toolkit.bmtp_dynamic_bridge", icon='PLAY')
        
        box = layout.box()
        box.label(text="模型分割", icon='MESH_PLANE')
        box.operator("toolkit.split_by_loose_part")
        box.operator("toolkit.split_mesh_by_common_vertex_group")
        box.operator("toolkit.split_by_vertex_group")


class BMTP_UVToolsPanel(bpy.types.Panel):
    bl_label = "UV数据工具"
    bl_idname = "VIEW3D_PT_Herta_BMTP_UVTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_ModelControl_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="UV清理", icon='UV_DATA')
        box.operator("toolkit.bmtp_keep_active_uv", icon='UV_SYNC_SELECT')
        box.separator()
        box.prop(props, "uv_delete_pattern")
        box.operator("toolkit.bmtp_delete_uvs_by_pattern", icon='X')
        
        box = layout.box()
        box.label(text="UV批量重命名", icon='OUTLINER_OB_GROUP_INSTANCE')
        box.prop(props, "uv_rename_old_pattern")
        box.prop(props, "uv_rename_new_template")
        box.operator("toolkit.bmtp_rename_uvs_by_pattern", icon='FONT_DATA')
        
        box = layout.box()
        box.label(text="UV批量添加", icon='ADD')
        box.prop(props, "uv_add_count")
        box.prop(props, "uv_add_name_template")
        box.operator("toolkit.bmtp_add_uv_layers", icon='ADD')


class BMTP_SceneCleanPanel(bpy.types.Panel):
    bl_label = "场景清理"
    bl_idname = "VIEW3D_PT_Herta_BMTP_SceneClean_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        
        box = layout.box()
        box.label(text="场景清理工具")
        col = box.column(align=True)
        col.operator("toolkit.bmtp_delete_empty_meshes")
        col.operator("toolkit.delete_loose_point")
        col.operator("toolkit.bmtp_clear_materials")
        col.operator("toolkit.bmtp_sync_data_names")
        col.operator("toolkit.bmtp_clean_useless_shape_keys")
        
        box = layout.box()
        box.label(text="独立清理项目")
        col = box.column(align=True)
        
        col.label(text="材质清理:")
        col.operator("toolkit.bmtp_clean_empty_material_slots", text="清理空材质槽")
        col.operator("toolkit.bmtp_clean_duplicate_materials", text="清理重复材质")
        
        col.separator()
        
        batch_row = col.row()
        batch_row.scale_y = 1.3
        batch_row.operator("toolkit.bmtp_batch_clear_all", text="批量清理所有属性", icon='TRASH')
        
        col.separator()
        
        col.label(text="折痕清理:")
        col.operator("toolkit.bmtp_clear_vertex_creases", text="清理顶点折痕")
        col.operator("toolkit.bmtp_clear_edge_creases", text="清理边线折痕")
        
        col.label(text="边线清理:")
        col.operator("toolkit.bmtp_clear_sharp_edges", text="清理锐边标记")
        col.operator("toolkit.bmtp_clear_seams", text="清理UV接缝标记")
        
        col.label(text="法线:")
        col.operator("toolkit.clear_custom_split_normals", text="清理自定义拆边法向")


class BMTP_CollectionLinkerPanel(bpy.types.Panel):
    bl_label = "集合关联工具"
    bl_idname = "VIEW3D_PT_Herta_BMTP_CollectionLinker_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 4
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        box = layout.box()
        box.label(text="第1步: 设置源项目 (可多选)", icon='ADD')
        box.operator("toolkit.bmtp_link_list_add", icon='EYEDROPPER')
        row = box.row()
        row.template_list("BMTP_UL_CollectionLinkList", "", props, "link_source_list", props, "link_source_list_index", rows=4)
        col = box.column(align=True)
        col.operator("toolkit.bmtp_link_list_remove", text="移除选中")
        col.operator("toolkit.bmtp_link_list_clear", text="全部清空")
        box = layout.box()
        box.label(text="第2步: 指定唯一目标集合", icon='COLLECTION_NEW')
        box.prop(props, "link_target_collection", text="")
        layout.separator()
        box = layout.box()
        box.label(text="第3步: 执行操作", icon='PLAY')
        row = box.row()
        row.scale_y = 1.5
        is_ready = bool(props.link_target_collection and props.link_source_list)
        row.enabled = is_ready
        row.operator("toolkit.bmtp_execute_collection_link", text="执行关联", icon='LINK_BLEND')
        row.operator("toolkit.bmtp_execute_collection_unlink", text="取消关联", icon='UNLINKED')
        if not is_ready:
            col = box.column(align=True)
            col.scale_y = 0.9
            if not props.link_source_list:
                col.label(text="提示: 源列表为空，请先添加项目。", icon='ERROR')
            if not props.link_target_collection:
                col.label(text="提示: 尚未指定目标集合。", icon='ERROR')


class BMTP_ModifierToolsPanel(bpy.types.Panel):
    bl_label = "修改器工具"
    bl_idname = "VIEW3D_PT_Herta_BMTP_ModifierTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_BMTP_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 5
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.bmtp_props
        
        box = layout.box()
        box.label(text="修改器与法线", icon='MODIFIER')
        box.operator("toolkit.apply_modifier_for_object_with_shape_keys")
        box.operator("toolkit.smooth_normal_save_to_uv")
        box.operator("toolkit.rename_amature_from_game")
        
        sub_box = box.box()
        sub_box.label(text="法线重计算", icon='MOD_NORMALEDIT')
        sub_box.operator("toolkit.recalculate_tangent_arithmetic_average_normal")
        sub_box.operator("toolkit.recalculate_color_arithmetic_average_normal")
        
        box = layout.box()
        box.label(text="修改器批量操作", icon='MODIFIER_DATA')
        
        col = box.column(align=True)
        col.operator("toolkit.bmtp_armature_to_shapekey")
        col.operator("toolkit.bmtp_apply_armature_modifier")
        col.operator("toolkit.bmtp_apply_all_shape_keys")
        
        col.separator()
        col.operator("toolkit.bmtp_lattice_to_shapekey")
        
        box.separator()
        box.prop(props, "mod_delete_names")
        box.operator("toolkit.bmtp_delete_modifiers_by_name", icon='X')
        
        box.separator()
        box.prop(props, "mod_apply_names")
        box.operator("toolkit.bmtp_apply_modifiers_by_name", icon='CHECKMARK')


class TT_MainPanel(bpy.types.Panel):
    bl_label = "贴图与材质工具"
    bl_idname = "VIEW3D_PT_Herta_TT_Main_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_Toolkit_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        from .tt_dependency_check import is_dependency_installed
        
        box = layout.box()
        col = box.column()
        if is_dependency_installed('scipy'): 
            col.label(text="依赖库 Scipy: 已安装", icon='CHECKMARK')
        else:
            col.label(text="依赖库 Scipy: 未安装!", icon='ERROR')
            col.label(text="法线贴图功能需要此库。")
            col.operator("toolkit.tt_ensure_dependencies", icon='CONSOLE')
        
        box = layout.box()
        box.label(text="全局输出目录", icon='PREFERENCES')
        box.prop(props, "output_dir", text="")


class TT_DDSConversionPanel(bpy.types.Panel):
    bl_label = "DDS贴图转换"
    bl_idname = "VIEW3D_PT_Herta_TT_DDS_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        import os
        import shutil
        
        config_box = layout.box()
        config_box.label(text="转换工具配置", icon='FILE_SCRIPT')
        col = config_box.column(align=True)
        
        from .tt_dds_conversion import TOOLSET_PATH, find_texconv
        found_path = find_texconv()
        found_method = ""
        
        if found_path:
            local_path = os.path.join(TOOLSET_PATH, 'texconv.exe')
            if found_path == local_path:
                found_method = "Toolset目录"
            elif props.texconv_path and found_path == props.texconv_path:
                found_method = "用户指定"
            else:
                found_method = "系统环境"
        
        if found_path:
            row = col.row(align=True)
            row.label(text="Texconv.exe: 已找到", icon='CHECKMARK')
            row.label(text=f" (方式: {found_method})")
        else:
            col.label(text="Texconv.exe: 未找到!", icon='ERROR')
            col.label(text="请放置到 Toolset 文件夹或手动指定。")
        col.prop(props, "texconv_path", text="手动指定")
        col.prop(props, "dds_delete_originals")
        col.separator()
        col.operator("toolkit.tt_convert_to_dds", icon='FILE_REFRESH')
        
        rules_box = layout.box()
        rules_box.label(text="DDS转换规则", icon='SETTINGS')
        
        rules_box.prop(props, "dds_use_custom_rules", text="使用自定义转换规则")
        
        if not props.dds_use_custom_rules:
            info_box = rules_box.box()
            info_box.label(text="当前使用默认规则:", icon='INFO')
            info_box.label(text="- DiffuseMap -> bc7_unorm_srgb")
            info_box.label(text="- NormalMap -> r8g8b8a8_unorm")
            info_box.label(text="- 其他 -> bc7_unorm")
        else:
            list_box = rules_box.box()
            list_box.label(text="转换规则列表", icon='SETTINGS')
            
            row = list_box.row()
            row.label(text="正则表达式")
            row.label(text="DDS格式")
            row.label(text="启用")
            
            for i, rule in enumerate(props.dds_rules):
                row = list_box.row()
                row.prop(rule, "pattern", text="")
                row.prop(rule, "format", text="")
                row.prop(rule, "enabled", text="")
                
                op = row.operator("toolkit.tt_remove_dds_rule", text="", icon='X', emboss=False)
                op.index = i
            
            row = list_box.row(align=True)
            row.operator("toolkit.tt_add_dds_rule", text="添加规则", icon='ADD')
            row.operator("toolkit.tt_reset_dds_rules", text="重置默认", icon='FILE_REFRESH')
            row.operator("toolkit.tt_test_dds_rule", text="测试规则", icon='VIEWZOOM')
            
            file_box = rules_box.box()
            file_box.label(text="规则文件操作", icon='FILE_SCRIPT')
            file_box.prop(props, "dds_rules_file_path", text="规则文件路径")
            row = file_box.row(align=True)
            row.operator("toolkit.tt_save_dds_rules", text="保存规则", icon='FILE_TICK')
            row.operator("toolkit.tt_load_dds_rules", text="加载规则", icon='FILE_FOLDER')


class TT_NormalMapPanel(bpy.types.Panel):
    bl_label = "法线贴图生成"
    bl_idname = "VIEW3D_PT_Herta_TT_NormalMap_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        from .tt_dependency_check import is_dependency_installed
        
        if not is_dependency_installed('scipy'):
            layout.box().label(text="请先安装依赖项 Scipy", icon='ERROR')
            return
        
        box = layout.box()
        box.label(text="生成参数", icon='SETTINGS')
        box.prop(props, "normal_map_strength")
        box.prop(props, "normal_map_blur_radius")
        box.prop(props, "normal_map_blue_channel_value")
        box.prop(props, "normal_map_invert")
        
        box = layout.box()
        box.label(text="材质选项", icon='MATERIAL')
        box.prop(props, "normal_map_create_materials")
        box.prop(props, "normal_map_material_prefix")
        
        layout.separator()
        layout.operator("toolkit.tt_generate_normal_maps", icon='EXPORT')


class TT_ColorBakePanel(bpy.types.Panel):
    bl_label = "通用节点烘焙"
    bl_idname = "VIEW3D_PT_Herta_TT_ColorBake_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        info_box = layout.box()
        info_box.label(text="重要提示:", icon='INFO')
        col = info_box.column(align=True)
        col.label(text="此功能为通用烘焙器。")
        col.label(text="输出文件名将直接使用材质名。")
        
        rules_box = layout.box()
        rules_box.label(text="烘焙分辨率规则", icon='SETTINGS')
        rules_box.prop(props, "bake_resolution_use_rules", text="使用材质名称匹配分辨率")
        
        if props.bake_resolution_use_rules:
            list_box = rules_box.box()
            list_box.label(text="分辨率规则列表", icon='SETTINGS')
            
            row = list_box.row()
            row.label(text="正则表达式")
            row.label(text="分辨率")
            row.label(text="启用")
            
            for i, rule in enumerate(props.bake_resolution_rules):
                row = list_box.row()
                row.prop(rule, "pattern", text="")
                row.prop(rule, "resolution", text="")
                row.prop(rule, "enabled", text="")
                
                op = row.operator("toolkit.tt_remove_bake_resolution_rule", text="", icon='X', emboss=False)
                op.index = i
            
            row = list_box.row(align=True)
            row.operator("toolkit.tt_add_bake_resolution_rule", text="添加规则", icon='ADD')
            row.operator("toolkit.tt_reset_bake_resolution_rules", text="重置默认", icon='FILE_REFRESH')
        
        box = layout.box()
        box.label(text="烘焙设置", icon='SETTINGS')
        row = box.row(align=True)
        row.prop(props, "color_bake_preview_type", expand=True)
        box.prop(props, "color_bake_size")
        box.prop(props, "color_bake_unfold_by_uv", text="按UV展开顶点")
        box.prop(props, "color_bake_node_types")
        box.prop(props, "color_bake_import_to_material")
        
        layout.separator()
        layout.operator("toolkit.tt_bake_color_maps", icon='RENDER_RESULT')


class TT_AlphaExtractPanel(bpy.types.Panel):
    bl_label = "透明通道提取"
    bl_idname = "VIEW3D_PT_Herta_TT_AlphaExtract_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        box = layout.box()
        box.label(text="提取设置", icon='SETTINGS')
        box.prop(props, "alpha_extract_allow_semitransparency")
        
        if not props.alpha_extract_allow_semitransparency:
            box.prop(props, "alpha_extract_threshold")
        
        box.separator()
        box.label(text="材质选项", icon='MATERIAL')
        box.prop(props, "alpha_extract_create_materials", text="创建透明材质")
        box.prop(props, "alpha_extract_material_prefix", text="新材质前缀")
        
        layout.separator()
        layout.operator("toolkit.tt_extract_alpha_channel", icon='EXPORT')


class TT_MaterialToolsPanel(bpy.types.Panel):
    bl_label = "材质批量操作"
    bl_idname = "VIEW3D_PT_Herta_TT_MaterialTools_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 4
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        assign_box = layout.box()
        assign_box.label(text="材质批量操作", icon='MATERIAL_DATA')
        assign_box.prop(props, "material_to_assign", text="")
        assign_box.label(text="用法: 先选材质, 再到视口中选物体")
        row = assign_box.row(align=True)
        row.operator("toolkit.tt_assign_material_to_selected", text="赋予材质", icon='ADD')
        row.operator("toolkit.tt_delete_material_from_selected", text="删除材质", icon='REMOVE')
        
        layout.separator()
        merge_box = layout.box()
        merge_box.label(text="清理重复材质", icon='BRUSH_DATA')
        col = merge_box.column(align=True)
        col.label(text="自动合并项目中所有使用相同贴图的材质。")
        col.label(text="(例如: Mat 和 Mat.001 将合并为 Mat)")
        merge_box.operator("toolkit.tt_merge_duplicate_materials", icon='AUTOMERGE_ON')


class TT_LightmapPanel(bpy.types.Panel):
    bl_label = "光照模板生成"
    bl_idname = "VIEW3D_PT_Herta_TT_Lightmap_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 5
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        info_box = layout.box()
        info_box.label(text="光照模板说明:", icon='INFO')
        col = info_box.column(align=True)
        col.label(text="为选中的网格物体创建LightMap和MaterialMap材质。")
        col.label(text="仅创建材质节点，不生成UV层。")
        
        box = layout.box()
        box.label(text="生成类型", icon='MATERIAL')
        row = box.row(align=True)
        row.prop(props, "lightmap_generate_lightmap")
        row.prop(props, "lightmap_generate_materialmap")
        
        box2 = layout.box()
        box2.label(text="应用模式", icon='SETTINGS')
        box2.prop(props, "lightmap_mode", expand=True)
        
        layout.separator()
        layout.operator("toolkit.tt_generate_lightmap_template", icon='MATERIAL')


class TT_MaterialPreviewPanel(bpy.types.Panel):
    bl_label = "材质预览与管理"
    bl_idname = "VIEW3D_PT_Herta_TT_MaterialPreview_Panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_parent_id = 'VIEW3D_PT_Herta_TT_Main_Panel'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 6
    
    def draw(self, context):
        layout = self.layout
        props = context.scene.texture_tools_props
        
        refresh_box = layout.box()
        refresh_box.label(text="材质匹配设置", icon='VIEWZOOM')
        refresh_box.prop(props, "material_preview_pattern", text="名称模式")
        refresh_box.operator("toolkit.tt_refresh_materials", icon='FILE_REFRESH')
        
        settings_box = layout.box()
        settings_box.label(text="全局设置", icon='SETTINGS')
        settings_box.prop(props, "material_preview_base_resolution")
        row = settings_box.row(align=True)
        row.operator("toolkit.tt_update_from_planes", icon='FILE_REFRESH')
        row.operator("toolkit.tt_clear_all_previews", icon='X')
        
        bake_box = layout.box()
        bake_box.label(text="烘焙贴图集", icon='RENDER_STILL')
        bake_box.operator("toolkit.tt_bake_atlas", icon='FILE_TICK')
        
        list_box = layout.box()
        list_box.label(text=f"材质列表 ({len(context.scene.material_preview_list)})", icon='MATERIAL_DATA')
        
        for i, item in enumerate(context.scene.material_preview_list):
            item_box = list_box.box()
            
            row = item_box.row(align=True)
            
            if item.is_visible:
                icon = 'HIDE_OFF'
            else:
                icon = 'HIDE_ON'
            
            op = row.operator("toolkit.tt_toggle_visibility", text="", icon=icon, emboss=False)
            op.index = i
            
            if item.material:
                row.label(text=item.material.name, icon='MATERIAL')
            else:
                row.label(text="无材质", icon='ERROR')
            
            select_op = row.operator("toolkit.tt_select_plane", text="", icon='RESTRICT_SELECT_OFF', emboss=False)
            select_op.index = i
            
            row = item_box.row(align=True)
            row.label(text=f"平面: {item.plane_object}", icon='OBJECT_DATAMODE')
            if item.source_objects:
                source_names = item.source_objects.split("|")
                source_text = f"源: {', '.join(source_names)}" if len(source_names) <= 3 else f"源: {', '.join(source_names[:3])}..."
                row.label(text=source_text, icon='OUTLINER_OB_MESH')
