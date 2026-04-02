import bpy


class GlobalProterties(bpy.types.PropertyGroup):
    open_mod_folder_after_generate_mod: bpy.props.BoolProperty(
        name="生成后打开Mod文件夹",
        description="勾选后，在生成Mod完成后自动打开Mod文件夹",
        default=True,
    ) # type: ignore

    zzz_use_slot_fix: bpy.props.BoolProperty(
        name="槽位风格贴图使用SlotFix技术",
        description="仅适用于槽位风格贴图，勾选后，特定名称标记的贴图将使用SlotFix风格，能一定程度上解决槽位风格贴图跨槽位的问题，跨Pixel槽位指的是在前一个DrawCall中是ps-t3但是下一个DrawCall变为ps-t5这种情况，但由于负责维护的人也在偷懒所以并不可靠",
        default=True,
    ) # type: ignore

    gimi_use_orfix: bpy.props.BoolProperty(
        name="槽位风格贴图使用ORFix",
        description="勾选后，在使用槽位风格贴图标记时，如果偷懒不想手动维护由于贴图槽位变化导致的贴图损坏问题，可以勾选此选项将问题交给ORFix维护者来解决，仅GIMI可用\n注意，如果你不懂ORFix和NNFix的原理，请不要取消勾选，取消勾选会严格按照贴图标记来执行贴图部分ini生成，默认你会在ini中自行写判断语句修复来替代实现ORFix和NNFix的功能",
        default=True,
    ) # type: ignore

    forbid_auto_texture_ini: bpy.props.BoolProperty(
        name="禁止自动贴图流程",
        description="生成Mod时禁止生成贴图相关ini部分",
        default=False,
    ) # type: ignore

    generate_branch_mod_gui: bpy.props.BoolProperty(
        name="生成分支切换Mod面板(测试版)",
        description="生成Mod时，生成一个基于当前集合架构的分支Mod面板，可在游戏中按住Ctrl + Alt呼出，仍在测试改进中",
        default=False,
    ) # type: ignore

    recalculate_tangent: bpy.props.BoolProperty(
        name="向量归一化法线存入TANGENT(全局)",
        description="使用向量相加归一化重计算所有模型的TANGENT值，勾选此项后无法精细控制具体某个模型是否计算，是偷懒选项,在不勾选时默认使用右键菜单中标记的选项。\n用途:\n1.一般用于修复GI角色,HI3 1.0角色,HSR角色轮廓线。\n2.用于修复模型由于TANGENT不正确导致的黑色色块儿问题，比如HSR的薄裙子可能会出现此问题。",
        default=False,
    ) # type: ignore

    recalculate_color: bpy.props.BoolProperty(
        name="算术平均归一化法线存入COLOR(全局)",
        description="使用算术平均归一化重计算所有模型的COLOR值，勾选此项后无法精细控制具体某个模型是否计算，是偷懒选项,在不勾选时默认使用右键菜单中标记的选项，仅用于HI3 2.0角色修复轮廓线",
        default=False,
    ) # type: ignore

    use_specific_generate_mod_folder_path: bpy.props.BoolProperty(
        name="生成Mod到指定的文件夹中",
        description="勾选后将生成Mod到你指定的文件夹中",
        default=False,
    ) # type: ignore

    generate_mod_folder_path: bpy.props.StringProperty(
        name="生成Mod文件夹路径",
        description="选择的生成Mod的文件夹路径",
        default="",
        subtype='DIR_PATH',
    ) # type: ignore

    use_mirror_workflow: bpy.props.BoolProperty(
        name="使用非镜像工作流",
        description="默认为False, 启用后导入和导出模型将不再是镜像的，目前3Dmigoto的模型导入后是镜像存粹是由于历史遗留问题是错误的，但是当错误积累成粑粑山，人的习惯和旧的工程很难被改变，所以只有勾选后才能使用非镜像工作流",
        default=False,
    ) # type: ignore

    use_normal_map: bpy.props.BoolProperty(
        name="自动上贴图时使用法线贴图",
        description="启用后在导入模型时自动附加法线贴图节点, 在材质预览模式下得到略微更好的视觉效果",
        default=False,
    ) # type: ignore

    import_merged_vgmap: bpy.props.BoolProperty(
        name="使用融合统一顶点组",
        description="导入时是否导入融合后的顶点组 (Unreal的合并顶点组技术会用到)，一般鸣潮Mod需要勾选来降低制作Mod的复杂度",
        default=True,
    ) # type: ignore

    ignore_muted_shape_keys: bpy.props.BoolProperty(
        name="忽略未启用的形态键",
        description="勾选此项后，未勾选启用的形态键在生成Mod时会被忽略，勾选的形态键会参与生成Mod",
        default=True,
    ) # type: ignore

    apply_all_modifiers: bpy.props.BoolProperty(
        name="应用所有修改器",
        description="勾选此项后，生成Mod之前会自动对物体应用所有修改器",
        default=False,
    ) # type: ignore

    import_skip_empty_vertex_groups: bpy.props.BoolProperty(
        name="跳过空顶点组",
        description="勾选此项后，导入时会跳过空的顶点组",
        default=True,
    ) # type: ignore

    export_add_missing_vertex_groups: bpy.props.BoolProperty(
        name="导出时添加缺失顶点组",
        description="勾选此项后，生成Mod时会自动重新排列并填补数字顶点组间的间隙空缺",
        default=True,
    ) # type: ignore

    @classmethod
    def _instance(cls):
        return bpy.context.scene.global_properties

    @classmethod
    def open_mod_folder_after_generate_mod(cls):
        return cls._instance().open_mod_folder_after_generate_mod

    @classmethod
    def zzz_use_slot_fix(cls):
        return cls._instance().zzz_use_slot_fix

    @classmethod
    def gimi_use_orfix(cls):
        return cls._instance().gimi_use_orfix

    @classmethod
    def forbid_auto_texture_ini(cls):
        return cls._instance().forbid_auto_texture_ini

    @classmethod
    def generate_branch_mod_gui(cls):
        return cls._instance().generate_branch_mod_gui

    @classmethod
    def recalculate_tangent(cls):
        return cls._instance().recalculate_tangent

    @classmethod
    def recalculate_color(cls):
        return cls._instance().recalculate_color

    @classmethod
    def use_specific_generate_mod_folder_path(cls):
        return cls._instance().use_specific_generate_mod_folder_path

    @classmethod
    def generate_mod_folder_path(cls):
        return cls._instance().generate_mod_folder_path

    @classmethod
    def use_mirror_workflow(cls):
        return cls._instance().use_mirror_workflow

    @classmethod
    def use_normal_map(cls):
        return cls._instance().use_normal_map

    @classmethod
    def import_merged_vgmap(cls):
        return cls._instance().import_merged_vgmap

    @classmethod
    def ignore_muted_shape_keys(cls):
        return cls._instance().ignore_muted_shape_keys

    @classmethod
    def apply_all_modifiers(cls):
        return cls._instance().apply_all_modifiers

    @classmethod
    def import_skip_empty_vertex_groups(cls):
        return cls._instance().import_skip_empty_vertex_groups

    @classmethod
    def export_add_missing_vertex_groups(cls):
        return cls._instance().export_add_missing_vertex_groups


def register():
    bpy.utils.register_class(GlobalProterties)
    bpy.types.Scene.global_properties = bpy.props.PointerProperty(type=GlobalProterties)


def unregister():
    del bpy.types.Scene.global_properties
    bpy.utils.unregister_class(GlobalProterties)