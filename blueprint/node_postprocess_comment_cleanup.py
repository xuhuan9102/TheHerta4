"""配置文件清理后处理节点。

导出时清理生成 INI 里的非 ASCII 文本；「应用清理到Mod」按钮走原地清理，
**不需要用户填路径**：目标配置表按其余后处理节点（动画驱动 / 文本追加 /
UI 面板）同口径从 Generate Mod 输出目录自动定位（见 ``_find_target_ini_file``）。
节点上的「INI文件」只作为多配置表目录下的手动兜底，留空即可。
"""

import bpy
import glob
import os

from ..common.config_table_backup import find_config_table_files
from ..common.global_config import GlobalConfig
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_material import SSMTNode_PostProcess_MaterialBase


def _read_mod_export_path():
    """读取当前 Generate Mod 输出目录；读不到就返回空串（由调用方报错）。"""
    try:
        GlobalConfig.read_from_main_json_ssmt4()
        return str(GlobalConfig.path_generate_mod_folder() or "").strip()
    except Exception as exc:
        print(f"读取 Generate Mod 输出目录失败: {exc}")
        return ""


class SSMT_OT_CommentCleanup_Refresh(bpy.types.Operator):
    """按当前导出目录原地清理已导出配置表中的非 ASCII 文本。"""

    bl_idname = "ssmt.comment_cleanup_refresh"
    bl_label = "应用清理到Mod"
    bl_description = "按当前导出目录中的配置表原地转换中文和非 ASCII 文本，无需重新导出整个 Mod"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if tree:
                node = tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_PostProcess_CommentCleanup':
                    target_ini, error_message = node.resolve_in_place_target(_read_mod_export_path())
                    if not target_ini:
                        self.report({'WARNING'}, error_message or "未找到目标配置表")
                        return {'CANCELLED'}

                    try:
                        ok = node.execute_postprocess(
                            os.path.dirname(target_ini),
                            _in_place=True,
                            _ini_path=target_ini,
                        )
                    except Exception as exc:
                        self.report({'ERROR'}, f"清理失败，请查看控制台日志: {exc}")
                        return {'CANCELLED'}

                    if ok:
                        self.report({'INFO'}, f"已完成配置文件清理: {os.path.basename(target_ini)}")
                        return {'FINISHED'}
                    self.report({'ERROR'}, "清理失败，请查看控制台日志")
                    return {'CANCELLED'}

        self.report({'WARNING'}, "无法找到配置文件清理节点")
        return {'CANCELLED'}


class SSMTNode_PostProcess_CommentCleanup(SSMTNode_PostProcess_Base):
    bl_idname = "SSMTNode_PostProcess_CommentCleanup"
    bl_label = "配置文件清理"
    bl_description = "导出时将生成的 INI 文件中的中文和其他非 ASCII 文本转换为英文标识"

    ini_file_path: bpy.props.StringProperty(
        name="INI文件",
        description="可选的手动兜底：导出目录里有多个配置表、无法自动确定目标时才需要填写",
        subtype='FILE_PATH',
        default="",
    )
    last_mod_ini_path: bpy.props.StringProperty(
        name="上次导出INI",
        description="最近一次导出时生成的 Mod INI 路径，供原地清理按钮兜底使用",
        default="",
        options={'HIDDEN'},
    )

    def draw_buttons(self, context, layout):
        layout.label(text="将INI中的中文和非ASCII文本转换为英文标识", icon="TEXT")
        box_top = layout.box()
        box_top.operator(
            "ssmt.comment_cleanup_refresh",
            text="应用清理到Mod（原地更新）",
            icon='FILE_TICK',
        ).node_name = self.name
        box_top.label(text="自动使用 Generate Mod 输出目录中的配置表", icon='FILE_REFRESH')
        box_top.prop(self, "ini_file_path", text="指定INI（可选）")
        if self.last_mod_ini_path:
            box_top.label(text=f"上次导出: {os.path.basename(self.last_mod_ini_path)}", icon='INFO')

    @staticmethod
    def _find_target_ini_file(mod_export_path):
        """在导出目录根目录定位配置表，返回 ``(路径, 错误信息)``。

        与动画驱动 / 文本追加 / UI 面板同口径：只有一个配置表就直接用它；
        有多个时按当前工作空间名消歧（先同名 ``<工作空间>.ini``，再
        ``<工作空间>_*.ini`` 前缀），仍不能唯一确定就报错并请用户手动指定。

        材质转资源pro 的纯贴图模式会额外生成 ``*_PureTexture.ini`` 派生产物，
        它不是主配置表：先排除再消歧；``find_config_table_files`` 本身不动，
        备份 / 清理流程仍能看到这些片段。
        """
        if not mod_export_path or not os.path.isdir(mod_export_path):
            return "", "当前导出目录不存在，请先确认 Generate Mod 输出路径"

        ini_files = find_config_table_files(mod_export_path)
        if not ini_files:
            return "", f"导出目录中未找到配置表（*.ini）: {mod_export_path}"

        filtered_files = [
            path for path in ini_files
            if not os.path.basename(path).casefold().endswith("_puretexture.ini")
        ]
        excluded_count = len(ini_files) - len(filtered_files)
        if excluded_count:
            print(
                "目标配置表定位：已排除材质转资源pro 的派生产物 "
                f"*_PureTexture.ini（{excluded_count} 个）"
            )
        ini_files = filtered_files
        if not ini_files:
            return "", (
                "导出目录中只剩材质转资源pro 的派生产物 *_PureTexture.ini，"
                f"没有可清理的主配置表: {mod_export_path}"
            )

        workspace_name = str(GlobalConfig.get_workspace_name() or "").strip()
        if workspace_name:
            exact_candidates = [
                path for path in ini_files
                if os.path.splitext(os.path.basename(path))[0] == workspace_name
            ]
            prefixed_candidates = [
                path for path in ini_files
                if os.path.basename(path).startswith(f"{workspace_name}_")
                and path not in exact_candidates
            ]
            candidates = exact_candidates + prefixed_candidates
            if len(candidates) == 1:
                return candidates[0], ""
            if len(candidates) > 1:
                names = ", ".join(sorted(os.path.basename(path) for path in candidates))
                return "", f"导出目录中存在多个匹配当前工作空间的配置表，请在节点「指定INI」中手动选择: {names}"

        if len(ini_files) == 1:
            return ini_files[0], ""

        names = ", ".join(sorted(os.path.basename(path) for path in ini_files))
        return "", f"导出目录中存在多个配置表，无法确定清理目标，请在节点「指定INI」中手动选择: {names}"

    def resolve_in_place_target(self, mod_export_path="", explicit_ini_path=""):
        """解析原地清理的目标配置表，返回 ``(绝对路径, 错误信息)``。

        优先级：显式传入 > 节点「指定INI」> 导出目录自动定位 > 上次导出记录。
        手动指定的路径失效时打印提示并继续自动定位，避免一条过期路径把按钮卡死；
        全部落空才报错。
        """
        manual = str(explicit_ini_path or "").strip() or str(getattr(self, "ini_file_path", "") or "").strip()
        if manual and os.path.isfile(manual):
            return os.path.abspath(manual), ""

        target_ini, error_message = self._find_target_ini_file(mod_export_path)
        if target_ini:
            if manual:
                print(f"指定的配置表不存在，已改用导出目录中的配置表: {manual}")
            return os.path.abspath(target_ini), ""

        fallback = str(getattr(self, "last_mod_ini_path", "") or "").strip()
        if fallback and os.path.isfile(fallback):
            return os.path.abspath(fallback), ""

        if manual:
            detail = f"；{error_message}" if error_message else ""
            return "", f"指定的配置表不存在: {manual}{detail}"
        return "", error_message or "未找到可清理的配置表，请先导出一次 Mod"

    @staticmethod
    def _clean_ini_file(ini_path):
        try:
            with open(ini_path, "r", encoding="utf-8-sig", newline="") as handle:
                content = handle.read()
            cleaned = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(content)
            if cleaned == content:
                return False, 0
            with open(ini_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(cleaned)
            return True, sum(1 for char in content if ord(char) > 127)
        except (OSError, UnicodeError) as exc:
            print(f"配置注释清理读取/写入失败 {ini_path}: {exc}")
            return False, 0

    def execute_postprocess(self, mod_export_path, _in_place=False, _ini_path=None):
        if _in_place:
            target_ini, error_message = self.resolve_in_place_target(mod_export_path, _ini_path)
            if not target_ini:
                print(f"无法执行原地清理：{error_message}")
                return False
            ini_paths = [target_ini]
            self.last_mod_ini_path = target_ini
        else:
            ini_paths = glob.glob(os.path.join(mod_export_path, "**", "*.ini"), recursive=True)
            # 记录导出目录里的配置表本体（不是子目录里的零散 ini），供下次原地清理兜底。
            root_ini_path, _error = self._find_target_ini_file(mod_export_path)
            if root_ini_path:
                self.last_mod_ini_path = os.path.abspath(root_ini_path)
            elif ini_paths:
                self.last_mod_ini_path = os.path.abspath(sorted(ini_paths)[0])

        changed_files = 0
        replaced_chars = 0
        for ini_path in ini_paths:
            changed, replaced = self._clean_ini_file(ini_path)
            if changed:
                changed_files += 1
                replaced_chars += replaced
        print(f"配置文件清理完成：处理 {changed_files} 个INI文件，替换 {replaced_chars} 个非ASCII字符。")
        return True


classes = (SSMT_OT_CommentCleanup_Refresh, SSMTNode_PostProcess_CommentCleanup)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
