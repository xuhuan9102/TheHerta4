import bpy


class Properties_WWMI(bpy.types.PropertyGroup):
    import_merged_vgmap:bpy.props.BoolProperty(
        name="使用融合统一顶点组",
        description="导入时是否导入融合后的顶点组 (Unreal的合并顶点组技术会用到)，一般鸣潮Mod需要勾选来降低制作Mod的复杂度",
        default=True
    ) # type: ignore

    @classmethod
    def import_merged_vgmap(cls):
        '''
        bpy.context.scene.properties_wwmi.import_merged_vgmap
        '''
        return bpy.context.scene.properties_wwmi.import_merged_vgmap

    ignore_muted_shape_keys:bpy.props.BoolProperty(
        name="忽略未启用的形态键",
        description="勾选此项后，未勾选启用的形态键在生成Mod时会被忽略，勾选的形态键会参与生成Mod",
        default=True
    ) # type: ignore

    # ignore_muted_shape_keys
    @classmethod
    def ignore_muted_shape_keys(cls):
        '''
        bpy.context.scene.properties_wwmi.ignore_muted_shape_keys
        '''
        return bpy.context.scene.properties_wwmi.ignore_muted_shape_keys
    
    # apply_all_modifiers
    apply_all_modifiers:bpy.props.BoolProperty(
        name="应用所有修改器",
        description="勾选此项后，生成Mod之前会自动对物体应用所有修改器",
        default=False
    ) # type: ignore
    
    @classmethod
    def apply_all_modifiers(cls):
        '''
        bpy.context.scene.properties_wwmi.apply_all_modifiers
        '''
        return bpy.context.scene.properties_wwmi.apply_all_modifiers
    
    # import_skip_empty_vertex_groups
    import_skip_empty_vertex_groups:bpy.props.BoolProperty(
        name="跳过空顶点组",
        description="勾选此项后，导入时会跳过空的顶点组",
        default=True
    ) # type: ignore

    @classmethod
    def import_skip_empty_vertex_groups(cls):
        '''
        bpy.context.scene.properties_wwmi.import_skip_empty_vertex_groups
        '''
        return bpy.context.scene.properties_wwmi.import_skip_empty_vertex_groups
    
    # export_add_missing_vertex_groups
    export_add_missing_vertex_groups:bpy.props.BoolProperty(
        name="导出时添加缺失顶点组",
        description="勾选此项后，生成Mod时会自动重新排列并填补数字顶点组间的间隙空缺",
        default=True
    ) # type: ignore

    @classmethod
    def export_add_missing_vertex_groups(cls):
        '''
        bpy.context.scene.properties_wwmi.export_add_missing_vertex_groups
        '''
        return bpy.context.scene.properties_wwmi.export_add_missing_vertex_groups
    
def register():
    bpy.utils.register_class(Properties_WWMI)
    bpy.types.Scene.properties_wwmi = bpy.props.PointerProperty(type=Properties_WWMI)

def unregister():
    del bpy.types.Scene.properties_wwmi
    bpy.utils.unregister_class(Properties_WWMI)
