'''
基础信息面板。
'''
import bpy
import os

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..common.marked_texture_material import build_missing_marked_materials
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.workspace_config_pruner import WorkspaceConfigPruner
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


class SSMT_OT_ToggleIgnoreTextureAlpha(bpy.types.Operator):
    bl_idname = "ssmt.toggle_ignore_texture_alpha"
    bl_label = "导入贴图时忽略透明度通道"
    bl_description = '开启后，一键导入透明材质时，贴图的 Alpha 模式会被设为"无"，使透明度通道始终输出 1（不透明），且不破坏着色器连接结构'

    def execute(self, context):
        new_value = GlobalProterties.toggle_ignore_texture_alpha()
        state_text = "已开启" if new_value else "已关闭"
        self.report({'INFO'}, f"导入贴图时忽略透明度通道: {state_text}")
        return {'FINISHED'}


class SSMT_OT_FillMarkedTextureMaterials(bpy.types.Operator):
    """给选中的旧物体补齐贴图材质。

    早先导入的物体只建了漫反射材质，缺其他类型；本操作按 SSMT 子网格标记
    （物体上的 ``3DMigoto:WorkspaceUniqueStr`` 直接查工作空间 SubmeshJson，
    与导出 mod 用的是同一份标记）补齐：槽 0 渲染材质 + 各类型规范材质。
    已有同名词同类型的材质原样保留，不重建节点。
    """

    bl_idname = "ssmt.fill_marked_texture_materials"
    bl_label = "给选中物体补齐贴图材质"
    bl_description = (
        "按 SSMT 子网格标记，给选中的物体补齐缺失的贴图材质"
        "（槽 0 渲染材质 + 各类型规范材质）；已有同名词同类型的材质原样保留"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        selected = getattr(context, "selected_objects", None) or ()
        return any(getattr(obj, "type", "") == 'MESH' for obj in selected)

    def execute(self, context):
        targets = [
            obj for obj in (getattr(context, "selected_objects", None) or ())
            if getattr(obj, "type", "") == 'MESH'
        ]
        if not targets:
            self.report({'WARNING'}, "请先选中至少一个网格物体")
            return {'CANCELLED'}

        created = 0
        reused = 0
        touched = 0
        warnings = []
        for obj in targets:
            try:
                obj_created, obj_reused, obj_warnings = build_missing_marked_materials(obj)
            except Exception as ex:
                warnings.append(f"{obj.name}: {ex}")
                continue
            created += obj_created
            reused += obj_reused
            if obj_created or obj_reused:
                touched += 1
            warnings.extend(f"{obj.name}: {item}" for item in obj_warnings)

        message = (
            f"补齐完成：{touched}/{len(targets)} 个物体，"
            f"新建 {created} 个材质，复用 {reused} 个"
        )
        if warnings:
            for item in warnings:
                print("[贴图标记] 补齐警告 " + item)
            message += f"，{len(warnings)} 条警告（见控制台）"
        self.report({'INFO'} if touched else {'WARNING'}, message)
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


def _prune_workspace_configs(lod_bare_pairs, dry_run: bool = False) -> dict:
    """清工作空间/游戏级配置里对这些部件的引用；返回 {相对路径: 删除条数}。

    lod_bare_pairs：只影响「意图类」文件（工作页行 / 别名表 / SkipIBConfig /
    VSCheckConfig）——传空集时它们一行都不删；「缓存类」文件（Import.json /
    ComponentName_DrawCallIndexList / DrawIB-Component / 贴图去重表 / 游戏级
    MarkTextureConfig）一律按磁盘现状判定，所以历史遗留的失效条目也能清掉。
    dry_run=True 只统计不写盘（确认弹窗预览用）。
    """
    return WorkspaceConfigPruner.prune(
        GlobalConfig.path_workspace_folder(),
        lod_bare_pairs,
        game_folder=GlobalConfig.path_current_game_total_workspace_folder(),
        dry_run=dry_run,
    )


def _deleted_lod_bare_pairs(records, deleted_paths) -> set[tuple[str, str]]:
    """记录里**确实删除成功**的 (lod_name, bare_name) 集合。"""
    deleted_keys = {os.path.normcase(str(path)) for path in deleted_paths}
    return {
        (record.get("lod_name", ""), record.get("bare_name", ""))
        for record in records
        if os.path.normcase(str(record.get("folder_path", ""))) in deleted_keys
    }


def _report_config_prune(removed: dict) -> str:
    """把清理结果打进控制台并返回给 report 的补充说明。"""
    for rel_path, count in sorted(removed.items()):
        print(f"[IB清理] 配置引用已清理: {rel_path}（{count} 条）")
    removed_total = sum(removed.values())
    if not removed_total:
        return ""
    return f"，并清理了 {len(removed)} 个配置文件里的 {removed_total} 条引用"


class SSMT_OT_CleanupUnusedIB(bpy.types.Operator):
    """基于当前场景剩余的 IB，删除工作空间中未使用 IB 的文件夹。

    全量提取会在工作空间生成大量无关的 IB 子网格文件夹；用户导入后手动清理
    （例如 30 个只留 10 个），但工作空间里那 20 个文件夹仍在，下次一键导入会
    再次全部导入。本算子以当前场景对象（3DMigoto:WorkspaceUniqueStr）为准，
    找出工作空间里未被场景引用的 IB 文件夹并直接删除，删除后一键导入即不再
    导入这些 IB。支持多 LOD：按 LOD 前缀精确匹配（LOD0/xxx 只被 LOD0.xxx 保留）。

    安全护栏：场景为空或没有任何对象能解析出 IB 身份时（保留集合为空），
    拒绝执行——此时“全部未保留”等于“清空整个工作空间”，属于不可逆的高风险
    操作，本插件不再提供任何一键清空入口（用户 2026-10-07 要求移除）。
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
        """返回 (待删记录列表, 场景保留身份数, 命中的工作空间文件夹数)。

        返回**记录**而不是路径：删完文件夹还要按 (lod_name, bare_name) 清配置引用
        （工作页行、别名表、SkipIBConfig、Import.json、游戏级贴图标记等）。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        if not workspace_root or not os.path.isdir(workspace_root):
            return [], 0, 0
        kept_pairs = self._collect_kept_lod_bare_pairs(context)
        if not kept_pairs:
            # 场景为空 / 身份解析失败：空集合会把"全部目录"当成待删目标，
            # 直接拒绝，绝不允许进入确认流程。
            return [], 0, 0
        targets = WorkSpaceHelper.get_unwanted_submesh_folder_records(kept_pairs)
        kept_folder_count = WorkSpaceHelper.count_kept_submesh_folders(kept_pairs)
        return targets, len(kept_pairs), kept_folder_count

    def invoke(self, context, event):
        records, kept_count, kept_folder_count = self._compute_targets(context)
        self._records = records
        self._targets = [record["folder_path"] for record in records]
        if kept_count == 0:
            self.report(
                {'ERROR'},
                "场景为空或没有可解析 IB 身份的对象，已拒绝清理；"
                "请先选中要保留的物体后重试",
            )
            return {'CANCELLED'}
        if self._targets and kept_folder_count == 0:
            self.report(
                {'ERROR'},
                "场景中的 IB 身份与当前工作空间没有任何匹配（疑似工作空间选错），"
                "已拒绝清理；请确认当前工作空间选择是否正确",
            )
            return {'CANCELLED'}
        # 预览：按"全部目标都删成功"估一下会清掉多少配置引用（dry_run 不写盘）
        self._config_preview = _prune_workspace_configs(
            {
                (record.get("lod_name", ""), record.get("bare_name", ""))
                for record in records
            },
            dry_run=True,
        )
        if not self._targets:
            if not self._config_preview:
                self.report({'INFO'}, "当前场景已包含工作空间中的全部 IB，无需清理")
                return {'FINISHED'}
            # 没有文件夹可删，但仍有历史遗留的失效配置引用（缓存类）→ 让用户确认
            return context.window_manager.invoke_confirm(self, event)
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        layout = self.layout
        preview = getattr(self, "_config_preview", None) or {}
        if self._targets:
            layout.label(text=f"将删除 {len(self._targets)} 个未使用的 IB 文件夹：")
            box = layout.box()
            for folder_path in self._targets[:10]:
                box.label(text="· " + os.path.basename(folder_path))
            if len(self._targets) > 10:
                box.label(text=f"… 等共 {len(self._targets)} 个")
        else:
            layout.label(text="没有可删除的 IB 文件夹，将只清理失效的配置引用：")
        preview_total = sum(preview.values())
        if preview_total:
            layout.label(
                text=f"并清理 {len(preview)} 个配置文件里的 {preview_total} 条引用：",
                icon='INFO',
            )
            preview_box = layout.box()
            for rel_path, count in sorted(preview.items())[:8]:
                preview_box.label(text=f"· {rel_path}（{count} 条）")
            if len(preview) > 8:
                preview_box.label(text=f"… 等共 {len(preview)} 个文件")
        layout.label(text="删除后再次一键导入将不再导入这些 IB", icon='INFO')

    def execute(self, context):
        records = getattr(self, "_records", None)
        if records is None:
            records, kept_count, kept_folder_count = self._compute_targets(context)
            self._records = records
            self._targets = [record["folder_path"] for record in records]
            if kept_count == 0:
                self.report(
                    {'ERROR'},
                    "场景为空或没有可解析 IB 身份的对象，已拒绝清理；"
                    "请先选中要保留的物体后重试",
                )
                return {'CANCELLED'}
            if kept_folder_count == 0:
                self.report(
                    {'ERROR'},
                    "场景中的 IB 身份与当前工作空间没有任何匹配（疑似工作空间选错），"
                    "已拒绝清理；请确认当前工作空间选择是否正确",
                )
                return {'CANCELLED'}
        targets = [record["folder_path"] for record in records]
        deleted_paths, failed_paths = WorkSpaceHelper.delete_folder_list(targets)
        for folder_path in failed_paths:
            self.report({'WARNING'}, f"删除失败 {os.path.basename(folder_path)}")
        print(f"[IB清理] 已按当前场景清理 {len(deleted_paths)}/{len(targets)} 个未使用 IB 文件夹")
        for folder_path in deleted_paths:
            print(f"[IB清理] 已删除: {folder_path}")
        for folder_path in failed_paths:
            print(f"[IB清理] 删除失败: {folder_path}")

        # 只按**确实删除成功**的部件清「意图类」行；缓存类文件按磁盘现状一并扫
        removed = _prune_workspace_configs(_deleted_lod_bare_pairs(records, deleted_paths))
        message = (
            f"已删除 {len(deleted_paths)} 个未使用的 IB 文件夹"
            + _report_config_prune(removed)
        )
        self.report({'INFO'}, message)
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

        # 蓝图选择是枚举属性（Blender 内部存枚举序号）：蓝图被重命名/删除，或
        # 旧存档里的序号对不上当前列表时，下拉框会显示空白、每次重绘刷
        # "current value ... matches no enum" 警告，删除/重命名/打开也会失去目标。
        # 这里先修复（写回一个确实存在的蓝图名）再取值，保证「下拉框显示的选择」
        # 和「按钮作用的目标」永远是同一个蓝图。
        selected_blueprint_name = (
            BlueprintExportHelper.ensure_valid_selected_blueprint_name(context=context)
            or BlueprintExportHelper.BLUEPRINT_NONE_IDENTIFIER
        )

        layout.label(text="TheHerta4 v4.4.51", icon='INFO')
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

        # 按 SSMT 标记添加贴图材质（默认开启）—— 紧跟在非镜像工作流下面
        layout.prop(global_properties, "import_materials_by_submesh_mark", icon='MATERIAL')

        # 导入贴图时忽略透明度通道 — 以按钮呈现，按下时表示已开启
        layout.operator(
            SSMT_OT_ToggleIgnoreTextureAlpha.bl_idname,
            text="导入贴图时忽略透明度通道",
            icon='IMAGE_ALPHA',
            depress=GlobalProterties.ignore_texture_alpha(),
        )

        # 导入后自动转为 PNG（一键导入完成后按帧处理：同名 .png 已存在就只把引用换过去，
        # 不重复转换；不存在才用 texconv 生成，避免重复导入反复转换卡顿）
        layout.prop(
            context.scene.texture_tools_props,
            "dds_auto_convert_png_after_import",
            icon='IMAGE_DATA',
        )

        # 给选中物体补齐贴图材质（按 SSMT 标记补齐缺失类型）
        layout.operator(SSMT_OT_FillMarkedTextureMaterials.bl_idname, icon='MATERIAL')

        # 基于当前场景剩余的 IB，删除工作空间中未使用 IB 的文件夹
        ib_cleanup_row = layout.row(align=True)
        ib_cleanup_row.operator(SSMT_OT_CleanupUnusedIB.bl_idname, text="清理未使用IB文件夹", icon='TRASH')

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

        # 布局保持原样：下拉框 + 重命名/删除两个图标按钮同一行（不显示文字）。
        # 「打开」只由下面的“打开蓝图界面”负责，不再重复一个图标入口。
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

        # Velo 工作空间入口固定显示；未切换到 Velo 时保持禁用。
        if hasattr(bpy.types, 'SSMT_OT_import_current_velo_workspace'):
            velo_row = layout.row(align=True)
            velo_row.enabled = global_properties.workspace_source_mode == 'VELO'
            velo_row.operator('ssmt.import_current_velo_workspace', text='导入当前velo工作空间', icon='IMPORT')

        if global_properties.expand_import_quick_tools:
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
        # ZZMI 专用实验开关：跨组融合统一顶点组测试。
        # 与上面已验证可用的合并骨骼**完全分离**（同组内合并不受影响、关闭本开关
        # 行为不变），仅用于跨 SkeletonGroup（对象变换不同）合并的实验与测试。
        if GlobalConfig.logic_name == LogicName.ZZMI:
            layout.prop(global_properties, "cross_group_merged_vgmap_test")
            # RedirectSO 跨 DrawIB 重定向在部分运行时顺序下会丢失合并几何，
            # 但仍按用户要求默认开启；保留显式开关用于出问题时对照关闭。
            # 关闭时仍使用合并骨架与单一合并对象。
            layout.prop(global_properties, "zzmi_merged_redirect_enabled")
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
    bpy.utils.register_class(SSMT_OT_ToggleIgnoreTextureAlpha)
    bpy.utils.register_class(SSMT_OT_FillMarkedTextureMaterials)
    bpy.utils.register_class(SSMT_OT_ClearMergedSkeletonCache)
    bpy.utils.register_class(SSMT_OT_CleanupUnusedIB)
    bpy.utils.register_class(PanelBasicInformation)


def unregister():
    bpy.utils.unregister_class(PanelBasicInformation)
    bpy.utils.unregister_class(SSMT_OT_CleanupUnusedIB)
    bpy.utils.unregister_class(SSMT_OT_ClearMergedSkeletonCache)
    bpy.utils.unregister_class(SSMT_OT_FillMarkedTextureMaterials)
    bpy.utils.unregister_class(SSMT_OT_ToggleIgnoreTextureAlpha)
    bpy.utils.unregister_class(SSMT4RefreshWorkspaceList)
    bpy.utils.unregister_class(SSMT_OT_ClearPreprocessCache)
