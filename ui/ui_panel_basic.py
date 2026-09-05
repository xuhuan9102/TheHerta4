'''
基础信息面板。
'''
import bpy
import os

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.workspace_helper import WorkSpaceHelper
from ..blueprint.export_helper import BlueprintExportHelper

from ..utils.translate_utils import TR

from .ui_func_import_ssmt import SSMT4ImportAllFromCurrentWorkSpaceBlueprint, SSMT4ImportRaw
from . import ui_prefix_quick_ops
from .ui_func_export import SSMTGenerateModBlueprint, SSMTQuickExportSelected

from ..blueprint.preprocess_cache import PreProcessCache
from ..blueprint.preprocess_parallel import ParallelPreprocessCoordinator


class SSMT_OT_ClearPreprocessCache(bpy.types.Operator):
    bl_idname = "ssmt.clear_preprocess_cache"
    bl_label = "清空前处理缓存"
    bl_description = "清空所有前处理缓存文件"

    def execute(self, context):
        cleared_count = PreProcessCache.clear_cache()
        self.report({'INFO'}, f"已清空 {cleared_count} 个缓存文件")
        return {'FINISHED'}


class SSMT4RefreshWorkspaceList(bpy.types.Operator):
    bl_idname = "ssmt4.refresh_workspace_list"
    bl_label = "刷新工作空间列表"
    bl_description = "刷新当前游戏配置下的工作空间列表"

    def execute(self, context):
        GlobalConfig.read_from_main_json_ssmt4()

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        self.report({'INFO'}, "已刷新工作空间列表")
        return {'FINISHED'}


class SSMT_OT_ToggleUseNormalMap(bpy.types.Operator):
    bl_idname = "ssmt.toggle_use_normal_map"
    bl_label = "自动上贴图时使用法线贴图"
    bl_description = "启用后在导入模型时自动附加法线贴图节点，在材质预览模式下得到略微更好的视觉效果"

    def execute(self, context):
        new_value = GlobalProterties.toggle_use_normal_map()
        state_text = "已开启" if new_value else "已关闭"
        self.report({'INFO'}, f"自动上贴图时使用法线贴图: {state_text}")
        return {'FINISHED'}


class SSMT_OT_ToggleIgnoreTextureAlpha(bpy.types.Operator):
    bl_idname = "ssmt.toggle_ignore_texture_alpha"
    bl_label = "导入贴图时忽略透明度通道"
    bl_description = '开启后，一键导入透明材质时，贴图的 Alpha 模式会被设为"无"，使透明度通道始终输出 1（不透明），且不破坏着色器连接结构'

    def execute(self, context):
        new_value = GlobalProterties.toggle_ignore_texture_alpha()
        state_text = "已开启" if new_value else "已关闭"
        self.report({'INFO'}, f"导入贴图时忽略透明度通道: {state_text}")
        return {'FINISHED'}


class SSMT_OT_ToggleStripTextureColorPrefix(bpy.types.Operator):
    bl_idname = "ssmt.toggle_strip_texture_color_prefix"
    bl_label = "导入贴图材质去掉颜色贴图前缀"
    bl_description = "开启后，从工作空间导入时创建的贴图材质名称不再携带颜色贴图（DiffuseMap）前缀，例如由 DiffuseMap_d892c658-2256-0 变为 d892c658-2256-0"

    def execute(self, context):
        new_value = GlobalProterties.toggle_import_texture_material_strip_color_prefix()
        state_text = "已开启" if new_value else "已关闭"
        self.report({'INFO'}, f"导入贴图材质去掉颜色贴图前缀: {state_text}")
        return {'FINISHED'}


class SSMT_OT_ClearMergedSkeletonCache(bpy.types.Operator):
    """清除骨骼合并 VGMap 缓存（单次确认）。

    本操作会删除工作空间所有子网格 json 的 VGMap/VGOffset/VGCount 缓存字段，
    删除后不可恢复，故先弹窗说明将清除的内容，确认后才真正执行。
    """
    bl_idname = "ssmt.clear_merged_skeleton_cache"
    bl_label = "清除骨骼合并VGMap缓存"
    bl_description = (
        "删除当前工作空间所有子网格 json 的 VGMap/VGOffset/VGCount 缓存；"
        "去重策略变更后旧缓存会被幂等跳过，清除后下次一键导入将按当前策略重新生成。"
        "EFMI（终末地）/ ZZMI（绝区零）模式下可用"
    )
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return GlobalConfig.logic_name in (LogicName.EFMI, LogicName.ZZMI)

    def _precheck(self, context):
        """模式 + 工作空间前置校验；返回错误信息字符串，无错误返回 None。"""
        if GlobalConfig.logic_name not in (LogicName.EFMI, LogicName.ZZMI):
            return "仅 EFMI（终末地）/ ZZMI（绝区零）模式下可用"
        workspace_root = GlobalConfig.path_workspace_folder()
        if not workspace_root or not os.path.isdir(workspace_root):
            return "当前工作空间目录无效，无法清理"
        return None

    def invoke(self, context, event):
        error = self._precheck(context)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        layout.label(text="将清除当前工作空间全部子网格 json 的骨骼合并缓存：")
        box = layout.box()
        box.label(text="· VGMap")
        box.label(text="· VGOffset")
        box.label(text="· VGCount")
        layout.label(
            text="清除后去重策略变更产生的旧缓存会被移除，"
            "下次一键导入将按当前策略重新生成",
            icon='INFO',
        )

    def execute(self, context):
        error = self._precheck(context)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        from ..common.efmi_skeleton import EFMISkeletonMergeHelper
        workspace_root = GlobalConfig.path_workspace_folder()
        cleaned, scanned = EFMISkeletonMergeHelper.clear_vgmap_cache(workspace_root)
        if cleaned > 0:
            message = (
                f"已清除 {cleaned} 个子网格的骨骼合并缓存（VGMap/VGOffset/VGCount），"
                "下次一键导入将重新生成"
            )
        else:
            message = f"扫描了 {scanned} 个 json，没有需要清理的 VGMap 缓存"
        print(f"[骨骼合并] {message}")
        self.report({'INFO'}, message)
        return {'FINISHED'}


class SSMT_OT_CleanupUnusedIB(bpy.types.Operator):
    """基于当前场景剩余的 IB，删除工作空间中未使用 IB 的文件夹。

    全量提取会在工作空间生成大量无关的 IB 子网格文件夹；用户导入后手动清理
    （例如 30 个只留 10 个），但工作空间里那 20 个文件夹仍在，下次一键导入会
    再次全部导入。本算子以当前场景对象（3DMigoto:WorkspaceUniqueStr）为准，
    找出工作空间里未被场景引用的 IB 文件夹并直接删除，删除后一键导入即不再
    导入这些 IB。支持多 LOD：按 LOD 前缀精确匹配（LOD0/xxx 只被 LOD0.xxx 保留）。

    安全护栏：场景为空或没有任何对象能解析出 IB 身份时（保留集合为空），
    拒绝执行——此时“全部未保留”等于“清空整个工作空间”，必须走
    SSMT_OT_ClearAllWorkspaceIB 的独立强确认操作。
    """
    bl_idname = "ssmt.cleanup_unused_ib"
    bl_label = "清理未使用IB文件夹"
    bl_description = (
        "以当前场景中剩余的 IB（子网格对象）为准，删除工作空间中其余未使用 IB 的文件夹；"
        "支持多 LOD（按 LOD 前缀匹配），删除后再次一键导入不会导入已删除的 IB"
    )
    bl_options = {'REGISTER'}

    def _collect_kept_lod_bare_pairs(self, context) -> set[tuple[str, str]]:
        kept_pairs = set()
        for obj in context.scene.objects:
            unique_str = str(obj.get("3DMigoto:WorkspaceUniqueStr", "") or "").strip()
            if unique_str:
                lod_name, bare_name = WorkSpaceHelper.parse_lod_unique_str(unique_str)
            else:
                # 兜底：无标记的对象（如 FMT 原始导入）按名称解析前缀
                prefix_info = ObjectPrefixHelper.extract_prefix_info(getattr(obj, "name", ""))
                if not prefix_info:
                    continue
                prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                lod_name = prefix_parts.get("lod_name", "")
                bare_name = prefix_parts.get("bare_unique_str", "")
            bare_name = str(bare_name or "").strip()
            if not bare_name:
                continue
            kept_pairs.add((str(lod_name or "").upper(), bare_name))
        return kept_pairs

    def _compute_targets(self, context):
        workspace_root = GlobalConfig.path_workspace_folder()
        if not workspace_root or not os.path.isdir(workspace_root):
            return [], 0, 0
        kept_pairs = self._collect_kept_lod_bare_pairs(context)
        if not kept_pairs:
            # 场景为空 / 身份解析失败：空集合会把"全部目录"当成待删目标，
            # 直接拒绝，绝不允许进入确认流程。
            return [], 0, 0
        targets = WorkSpaceHelper.get_unwanted_submesh_folder_list(kept_pairs)
        kept_folder_count = WorkSpaceHelper.count_kept_submesh_folders(kept_pairs)
        return targets, len(kept_pairs), kept_folder_count

    def invoke(self, context, event):
        targets, kept_count, kept_folder_count = self._compute_targets(context)
        self._targets = targets
        if kept_count == 0:
            self.report(
                {'ERROR'},
                "场景为空或没有可解析 IB 身份的对象，已拒绝清理；"
                "如需清空整个工作空间的 IB 文件夹，请使用「清空全部」",
            )
            return {'CANCELLED'}
        if targets and kept_folder_count == 0:
            self.report(
                {'ERROR'},
                "场景中的 IB 身份与当前工作空间没有任何匹配（疑似工作空间选错），"
                "已拒绝清理；确认要删除工作空间全部 IB 文件夹请使用「清空全部」",
            )
            return {'CANCELLED'}
        if not targets:
            self.report({'INFO'}, "当前场景已包含工作空间中的全部 IB，无需清理")
            return {'FINISHED'}
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        layout = self.layout
        layout.label(text=f"将删除 {len(self._targets)} 个未使用的 IB 文件夹：")
        box = layout.box()
        for folder_path in self._targets[:10]:
            box.label(text="· " + os.path.basename(folder_path))
        if len(self._targets) > 10:
            box.label(text=f"… 等共 {len(self._targets)} 个")
        layout.label(text="删除后再次一键导入将不再导入这些 IB", icon='INFO')

    def execute(self, context):
        targets = getattr(self, "_targets", None)
        if not targets:
            targets, kept_count, kept_folder_count = self._compute_targets(context)
            self._targets = targets
            if kept_count == 0:
                self.report(
                    {'ERROR'},
                    "场景为空或没有可解析 IB 身份的对象，已拒绝清理；"
                    "如需清空整个工作空间的 IB 文件夹，请使用「清空全部」",
                )
                return {'CANCELLED'}
            if kept_folder_count == 0:
                self.report(
                    {'ERROR'},
                    "场景中的 IB 身份与当前工作空间没有任何匹配（疑似工作空间选错），"
                    "已拒绝清理；确认要删除工作空间全部 IB 文件夹请使用「清空全部」",
                )
                return {'CANCELLED'}
        deleted_paths, failed_paths = WorkSpaceHelper.delete_folder_list(targets)
        for folder_path in failed_paths:
            self.report({'WARNING'}, f"删除失败 {os.path.basename(folder_path)}")
        print(f"[IB清理] 已按当前场景清理 {len(deleted_paths)}/{len(targets)} 个未使用 IB 文件夹")
        for folder_path in deleted_paths:
            print(f"[IB清理] 已删除: {folder_path}")
        for folder_path in failed_paths:
            print(f"[IB清理] 删除失败: {folder_path}")
        self.report({'INFO'}, f"已删除 {len(deleted_paths)} 个未使用的 IB 文件夹")
        return {'FINISHED'}


class SSMT_OT_ClearAllWorkspaceIB(bpy.types.Operator):
    """清空整个工作空间的全部 IB 子网格文件夹（独立强确认操作）。

    与 SSMT_OT_CleanupUnusedIB（按场景保留集合清理）完全分离：本算子不依赖
    场景身份解析，默认删除工作空间中每一个子网格文件夹。为避免误触，
    需要额外的「我确认」勾选框才会真正执行。
    """
    bl_idname = "ssmt.clear_all_workspace_ib"
    bl_label = "清空全部IB文件夹"
    bl_description = (
        "删除当前工作空间中全部 IB 子网格文件夹（清空整个工作空间的 IB 内容，"
        "不可恢复；删除后一键导入将不再导入任何 IB）"
    )
    bl_options = {'REGISTER'}

    confirm_wipe: bpy.props.BoolProperty(
        name="我确认清空工作空间全部IB文件夹",
        description="勾选后才会真正删除；此操作不可恢复",
        default=False,
    )

    def _compute_all_targets(self, context):
        workspace_root = GlobalConfig.path_workspace_folder()
        if not workspace_root or not os.path.isdir(workspace_root):
            return []
        return WorkSpaceHelper.get_all_submesh_folder_list()

    def invoke(self, context, event):
        self.confirm_wipe = False  # 每次弹窗都从“未确认”开始，防止属性残留直接放行
        self._targets = self._compute_all_targets(context)
        if not self._targets:
            self.report({'INFO'}, "工作空间中没有可删除的 IB 子网格文件夹")
            return {'FINISHED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.alert = True
        layout.label(
            text=f"将清空工作空间中全部 {len(self._targets)} 个 IB 文件夹，此操作不可恢复！",
            icon='ERROR',
        )
        box = layout.box()
        for folder_path in self._targets[:10]:
            box.label(text="· " + os.path.basename(folder_path))
        if len(self._targets) > 10:
            box.label(text=f"… 等共 {len(self._targets)} 个")
        layout.prop(self, "confirm_wipe")

    def execute(self, context):
        if not getattr(self, "confirm_wipe", False):
            self.report({'ERROR'}, "未勾选确认项，已取消清空操作")
            return {'CANCELLED'}
        targets = getattr(self, "_targets", None)
        if not targets:
            targets = self._compute_all_targets(context)
        deleted_paths, failed_paths = WorkSpaceHelper.delete_folder_list(targets)
        for folder_path in failed_paths:
            self.report({'WARNING'}, f"删除失败 {os.path.basename(folder_path)}")
        print(f"[IB清理] 清空工作空间: 已删除 {len(deleted_paths)}/{len(targets)} 个 IB 文件夹")
        for folder_path in deleted_paths:
            print(f"[IB清理] 已删除: {folder_path}")
        for folder_path in failed_paths:
            print(f"[IB清理] 删除失败: {folder_path}")
        self.report({'INFO'}, f"已清空 {len(deleted_paths)} 个 IB 文件夹")
        return {'FINISHED'}


class PanelBasicInformation(bpy.types.Panel):
    bl_label = "基础信息"
    bl_idname = "VIEW3D_PT_SSMT4_Basic_Information"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'TheHerta4'
    bl_order = 0

    @classmethod
    def poll(cls, context):
        if not hasattr(context.scene, 'herta_show_toolkit'):
            return True
        return not context.scene.herta_show_toolkit

    def draw(self, context):
        layout = self.layout
        global_properties = context.scene.global_properties

        GlobalConfig.read_from_main_json_ssmt4()

        selected_blueprint_name = (
            BlueprintExportHelper.get_preferred_blueprint_name(
                selected_name=getattr(global_properties, "selected_blueprint_name", ""),
                context=context,
            )
            or getattr(global_properties, "selected_blueprint_name", "")
            or BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER
        )

        layout.label(text="TheHerta4 v4.4.40", icon='INFO')
        layout.label(text=TR.translate("SSMT缓存文件夹路径: ") + GlobalConfig.ssmtlocation)
        layout.label(text=TR.translate("当前配置名称: ") + GlobalConfig.gamename)
        layout.label(text=TR.translate("当前游戏预设: ") + GlobalConfig.logic_name)
        layout.label(text=TR.translate("当前工作空间: ") + GlobalConfig.get_workspace_name())

        if len(context.selected_objects) != 0:
            obj = context.selected_objects[0]

            gametypename = obj.get("3DMigoto:GameTypeName", "")
            recalculate_tangent = obj.get("3DMigoto:RecalculateTANGENT", False)
            recalculate_color = obj.get("3DMigoto:RecalculateCOLOR", False)

            layout.label(text="GameType: " + gametypename)
            layout.label(text="RecalculateTANGENT: " + str(recalculate_tangent))
            layout.label(text="RecalculateCOLOR: " + str(recalculate_color))

        layout.prop(context.scene, "herta_show_toolkit", text="工具集模式", icon='TOOL_SETTINGS')
        if context.scene.herta_show_toolkit:
            layout.operator("model.switch_to_main_panel", text="返回主面板", icon='BACK')

        layout.prop(global_properties, "enable_non_mirror_workflow", text="非镜像工作流", toggle=True)

        # 导入贴图材质去掉颜色贴图前缀 — 以按钮呈现，按下时表示已开启
        layout.operator(
            SSMT_OT_ToggleStripTextureColorPrefix.bl_idname,
            text="导入贴图材质去掉颜色贴图前缀",
            icon='COLOR',
            depress=GlobalProterties.import_texture_material_strip_color_prefix(),
        )

        # 自动上贴图时使用法线贴图 — 以按钮呈现，按下时表示已开启
        layout.operator(
            SSMT_OT_ToggleUseNormalMap.bl_idname,
            text="自动上贴图时使用法线贴图",
            icon='NORMALS_FACE',
            depress=GlobalProterties.use_normal_map(),
        )

        # 导入贴图时忽略透明度通道 — 以按钮呈现，按下时表示已开启
        layout.operator(
            SSMT_OT_ToggleIgnoreTextureAlpha.bl_idname,
            text="导入贴图时忽略透明度通道",
            icon='IMAGE_ALPHA',
            depress=GlobalProterties.ignore_texture_alpha(),
        )

        # 基于当前场景剩余的 IB，删除工作空间中未使用 IB 的文件夹
        ib_cleanup_row = layout.row(align=True)
        ib_cleanup_row.operator(SSMT_OT_CleanupUnusedIB.bl_idname, text="清理未使用IB文件夹", icon='TRASH')
        # 独立强确认操作：清空整个工作空间全部 IB（不依赖场景身份解析）
        ib_cleanup_row.operator(SSMT_OT_ClearAllWorkspaceIB.bl_idname, text="清空全部", icon='ERROR')

        workspace_box = layout.box()
        workspace_box.label(text="工作空间来源", icon='FILE_FOLDER')
        workspace_box.prop(global_properties, "workspace_source_mode")
        if global_properties.workspace_source_mode == "SPECIFIC":
            workspace_row = workspace_box.row(align=True)
            workspace_row.prop(global_properties, "specific_workspace_name", text="指定工作空间")
            workspace_row.operator(SSMT4RefreshWorkspaceList.bl_idname, text="", icon='FILE_REFRESH')
        elif global_properties.workspace_source_mode == "CUSTOM":
            workspace_box.prop(global_properties, "custom_workspace_folder_path", text="自定义目录")

        layout.separator()

        blueprint_box = layout.box()
        blueprint_box.label(text="蓝图", icon='NODETREE')

        blueprint_row = blueprint_box.row(align=True)
        blueprint_row.prop(global_properties, "selected_blueprint_name", text="SSMT蓝图")

        rename_operator = blueprint_row.operator(
            "theherta3.rename_persistent_blueprint",
            text="",
            icon='GREASEPENCIL',
        )
        rename_operator.blueprint_name = selected_blueprint_name

        delete_operator = blueprint_row.operator(
            "theherta3.delete_persistent_blueprint",
            text="",
            icon='TRASH',
        )
        delete_operator.blueprint_name = selected_blueprint_name

        open_operator = blueprint_row.operator(
            "theherta3.open_persistent_blueprint",
            text="",
            icon='NODETREE',
        )
        open_operator.blueprint_name = (
            selected_blueprint_name
            if selected_blueprint_name != BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER
            else ""
        )

        open_current = blueprint_box.operator(
            "theherta3.open_persistent_blueprint",
            text="打开蓝图界面",
            icon='NODETREE',
        )
        open_current.blueprint_name = (
            selected_blueprint_name
            if selected_blueprint_name != BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER
            else ""
        )

        generate_operator = blueprint_box.operator(
            SSMTGenerateModBlueprint.bl_idname,
            text="生成所选蓝图 Mod",
            icon='EXPORT',
        )
        generate_operator.blueprint_name = selected_blueprint_name

        layout.separator()

        layout.operator(SSMTQuickExportSelected.bl_idname, text="快速局部导出", icon='EXPORT')

        import_row = layout.row(align=True)
        import_row.operator(SSMT4ImportAllFromCurrentWorkSpaceBlueprint.bl_idname, text="一键导入SSMT工作空间内容", icon='IMPORT')
        import_row.prop(
            global_properties,
            "expand_import_quick_tools",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_import_quick_tools else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )

        if global_properties.expand_import_quick_tools:
            velo_row = layout.row()
            velo_row.enabled = global_properties.workspace_source_mode == 'VELO'
            velo_row.operator('ssmt.import_current_velo_workspace', text='导入当前velo工作空间', icon='IMPORT')
            import_box = layout.box()
            import_box.operator("import_mesh.migoto_raw_buffers_mmt", text="导入FMT格式模型", icon='IMPORT')
            import_box.operator(SSMT4ImportRaw.bl_idname, text="导入SSMT格式模型", icon='IMPORT')

        ui_prefix_quick_ops.draw_prefix_quick_section(layout, context)

        layout.separator()

        cache_box = layout.box()
        cache_header = cache_box.row(align=True)
        cache_header.prop(
            global_properties,
            "expand_preprocess_cache",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_preprocess_cache else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        cache_header.label(text="前处理缓存", icon='FILE_CACHE')

        if global_properties.expand_preprocess_cache:
            cache_box.prop(global_properties, "enable_preprocess_cache")

            cache_stats = PreProcessCache.get_cache_stats()
            file_count = cache_stats["file_count"]
            total_size = cache_stats["total_size"]
            size_str = PreProcessCache.format_size(total_size)
            cache_box.label(text=f"缓存文件: {file_count} 个 大小: {size_str}")

            row = cache_box.row()
            row.operator(SSMT_OT_ClearPreprocessCache.bl_idname, icon='TRASH')

        parallel_box = layout.box()
        parallel_header = parallel_box.row(align=True)
        parallel_header.prop(
            global_properties,
            "expand_parallel_processing",
            text="",
            icon='TRIA_DOWN' if global_properties.expand_parallel_processing else 'TRIA_RIGHT',
            icon_only=True,
            emboss=False,
        )
        parallel_header.label(text="并行处理", icon='SYSTEM')

        if global_properties.expand_parallel_processing:
            parallel_box.prop(global_properties, "enable_parallel_preprocess")
            parallel_box.prop(global_properties, "enable_parallel_export_rounds")

            if global_properties.enable_parallel_preprocess or global_properties.enable_parallel_export_rounds:
                parallel_box.prop(global_properties, "parallel_blender_executable")
                parallel_box.prop(global_properties, "parallel_preprocess_instances")
                parallel_box.prop(global_properties, "parallel_preprocess_timeout_seconds")
                parallel_box.prop(global_properties, "parallel_preprocess_keep_temp_files")

                effective_path = ParallelPreprocessCoordinator.get_effective_blender_executable()
                display_path = os.path.basename(effective_path) if effective_path else "未设置"
                is_valid, message = ParallelPreprocessCoordinator.get_validation_summary()

                parallel_box.label(text=f"当前生效路径: {display_path}")
                parallel_box.label(text=message, icon='CHECKMARK' if is_valid else 'ERROR')

        # 骨骼合并复选框（import_merged_vgmap，「使用融合统一顶点组」）：
        # WWMI（融合统一顶点组）/ ZZMI / EFMI（骨骼合并，Merged Skeleton）共用同一把开关；
        # 勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 完全维持原路线（见 ZZMI骨骼合并计划书.md §5.1）。
        if GlobalConfig.logic_name in (LogicName.WWMI, LogicName.ZZMI, LogicName.EFMI):
            layout.prop(global_properties, "import_merged_vgmap")
        # EFMI 专用：多 LOD 使用 LOD0 分组投影，关闭则两侧独立去重。
        if GlobalConfig.logic_name == LogicName.EFMI:
            layout.prop(global_properties, "efmi_lod_group_projection")
            # EFMI 顶点组去重开关：关闭时不执行权重扩散去重（恒等映射，
            # 每根骨骼独占槽位），用于去重误并/偏移诊断与回滚。
            layout.prop(global_properties, "efmi_lod_group_dedup")

        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            layout.prop(global_properties, "import_skip_empty_vertex_groups")

        # 骨骼合并（EFMI/ZZMI）：一键清除子网格 json 里缓存的 VGMap（去重策略变更后强制重生成）
        if GlobalConfig.logic_name in (LogicName.EFMI, LogicName.ZZMI):
            layout.operator(SSMT_OT_ClearMergedSkeletonCache.bl_idname, icon='TRASH')


def register():
    bpy.utils.register_class(SSMT_OT_ClearPreprocessCache)
    bpy.utils.register_class(SSMT4RefreshWorkspaceList)
    bpy.utils.register_class(SSMT_OT_ToggleUseNormalMap)
    bpy.utils.register_class(SSMT_OT_ToggleIgnoreTextureAlpha)
    bpy.utils.register_class(SSMT_OT_ToggleStripTextureColorPrefix)
    bpy.utils.register_class(SSMT_OT_ClearMergedSkeletonCache)
    bpy.utils.register_class(SSMT_OT_CleanupUnusedIB)
    bpy.utils.register_class(SSMT_OT_ClearAllWorkspaceIB)
    bpy.utils.register_class(PanelBasicInformation)


def unregister():
    bpy.utils.unregister_class(PanelBasicInformation)
    bpy.utils.unregister_class(SSMT_OT_ClearAllWorkspaceIB)
    bpy.utils.unregister_class(SSMT_OT_CleanupUnusedIB)
    bpy.utils.unregister_class(SSMT_OT_ClearMergedSkeletonCache)
    bpy.utils.unregister_class(SSMT_OT_ToggleStripTextureColorPrefix)
    bpy.utils.unregister_class(SSMT_OT_ToggleIgnoreTextureAlpha)
    bpy.utils.unregister_class(SSMT_OT_ToggleUseNormalMap)
    bpy.utils.unregister_class(SSMT4RefreshWorkspaceList)
    bpy.utils.unregister_class(SSMT_OT_ClearPreprocessCache)
