import bpy
import os
import glob
import datetime
import shutil

from bpy.types import Node
from bpy.props import StringProperty

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .anim_driver_base import SSMTNode_AnimDriver_Base
from .anim_driver_collector import AnimationDriverCollector
from .fluid_ini_merge import mark_emitter_gate_writes
from ..common.global_config import GlobalConfig

_ANIM_DRIVER_SECTION_MARKER_START = "; --- ANIMATION DRIVER SECTION ---"
_ANIM_DRIVER_SECTION_MARKER_END = "; --- END ANIMATION DRIVER SECTION ---"

_ANIM_DRIVER_BLUEPRINT_BASE_NAME = "动画驱动蓝图"


def _create_unique_anim_driver_blueprint_name() -> str:
    if _ANIM_DRIVER_BLUEPRINT_BASE_NAME not in bpy.data.node_groups:
        return _ANIM_DRIVER_BLUEPRINT_BASE_NAME

    index = 1
    while True:
        name = f"{_ANIM_DRIVER_BLUEPRINT_BASE_NAME}_{index:03d}"
        if name not in bpy.data.node_groups:
            return name
        index += 1


class SSMT_OT_CreateAnimDriverBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.create_anim_driver_blueprint"
    bl_label = "新建动画驱动蓝图"
    bl_description = "创建一个新的动画驱动蓝图，仅允许放置动画驱动节点"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: StringProperty(
        name="Node Name",
        description="关联的后处理节点名称",
        default="",
    )

    def execute(self, context):
        blueprint_name = _create_unique_anim_driver_blueprint_name()
        new_tree = bpy.data.node_groups.new(name=blueprint_name, type='SSMTBlueprintTreeType')
        new_tree.use_fake_user = True
        new_tree["is_animation_driver"] = True

        if self.node_name:
            for tree in bpy.data.node_groups:
                if tree.bl_idname != 'SSMTBlueprintTreeType':
                    continue
                for node in tree.nodes:
                    if node.name == self.node_name and hasattr(node, 'blueprint_name'):
                        node.blueprint_name = blueprint_name
                        node.label = f"动画驱动: {blueprint_name}"
                        break

        self.report({'INFO'}, f"已创建动画驱动蓝图: {blueprint_name}")
        return {'FINISHED'}


class SSMT_OT_RefreshAnimDriverExportSection(bpy.types.Operator):
    bl_idname = "ssmt.refresh_anim_driver_export_section"
    bl_label = "刷新已导出动画驱动"
    bl_description = "不重新导出整个 Mod，仅按当前动画驱动蓝图重写已导出 INI 中的动画驱动段"
    bl_options = {'REGISTER'}

    node_name: StringProperty(
        name="Node Name",
        description="关联的动画驱动后处理节点名称",
        default="",
    )

    def execute(self, context):
        tree = getattr(getattr(context, "space_data", None), "edit_tree", None)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图编辑上下文")
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if node is None or getattr(node, "bl_idname", "") != 'SSMTNode_PostProcess_AnimDriver':
            self.report({'ERROR'}, "未找到动画驱动后处理节点")
            return {'CANCELLED'}

        GlobalConfig.read_from_main_json_ssmt4()
        mod_export_path = str(GlobalConfig.path_generate_mod_folder() or "").strip()
        if not mod_export_path or not os.path.isdir(mod_export_path):
            self.report({'ERROR'}, "当前导出目录不存在，请先确认 Generate Mod 输出路径")
            return {'CANCELLED'}

        success, message = node.refresh_exported_anim_driver_section(mod_export_path)
        report_level = {'INFO'} if success else {'ERROR'}
        self.report(report_level, message)
        return {'FINISHED'} if success else {'CANCELLED'}


class SSMTNode_PostProcess_AnimDriver(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_AnimDriver'
    bl_label = '动画驱动蓝图'
    bl_description = '引用动画驱动蓝图，将其收集构建的INI配置插入到配置表最上方'
    bl_icon = 'ACTION'

    def update_blueprint_name(self, context):
        if self.blueprint_name and self.blueprint_name != 'NONE':
            self.label = f"动画驱动: {self.blueprint_name}"
        else:
            self.label = "动画驱动蓝图"
        self.update_node_width([self.blueprint_name])

    blueprint_name: StringProperty(
        name="蓝图名称",
        description="选择要引用的动画驱动蓝图",
        default="",
        update=update_blueprint_name,
    )

    def init(self, context):
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.outputs.new('SSMTSocketPostProcess', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)
        row.prop_search(self, "blueprint_name", bpy.data, "node_groups", text="", icon='NODETREE')

        op = row.operator("ssmt.create_anim_driver_blueprint", text="", icon='ADD')
        op.node_name = self.name

        if self.blueprint_name and self.blueprint_name != 'NONE':
            blueprint = bpy.data.node_groups.get(self.blueprint_name)
            if blueprint and blueprint.bl_idname == 'SSMTBlueprintTreeType' and blueprint.get("is_animation_driver"):
                box = layout.box()
                box.label(text=f"节点数: {len(blueprint.nodes)}", icon='NODE')
                box.label(text=f"连接数: {len(blueprint.links)}", icon='LINKED')

                driver_node_count = sum(
                    1 for n in blueprint.nodes
                    if hasattr(n, "generate_ini_segment") and callable(n.generate_ini_segment)
                    and n.bl_idname != 'SSMTNode_AnimDriver_Base'
                )
                box.label(text=f"驱动节点: {driver_node_count}", icon='ACTION')

                if driver_node_count > 0:
                    collector = AnimationDriverCollector(blueprint)
                    paragraph_count = collector.count_paragraphs()
                    box.label(text=f"段落数: {paragraph_count}", icon='FILE_TEXT')

                box.separator()
                row = box.row(align=True)
                row.operator("ssmt.blueprint_nest_navigate", text="进入动画驱动蓝图", icon='FORWARD')
                refresh_op = row.operator("ssmt.refresh_anim_driver_export_section", text="刷新已导出驱动", icon='FILE_REFRESH')
                refresh_op.node_name = self.name
            elif blueprint:
                box = layout.box()
                box.label(text="警告: 选中的不是SSMT蓝图", icon='ERROR')
            else:
                box = layout.box()
                box.label(text="警告: 蓝图不存在", icon='ERROR')

    def _get_anim_driver_blueprint(self):
        if not self.blueprint_name or self.blueprint_name == 'NONE':
            return None, "未选择动画驱动蓝图"

        blueprint = bpy.data.node_groups.get(self.blueprint_name)
        if not blueprint or blueprint.bl_idname != 'SSMTBlueprintTreeType':
            return None, f"蓝图 '{self.blueprint_name}' 不存在或不是SSMT蓝图"

        if not blueprint.get("is_animation_driver"):
            return None, f"蓝图 '{self.blueprint_name}' 不是动画驱动蓝图"

        return blueprint, ""

    @staticmethod
    def _has_incomplete_anim_driver_section(content: str) -> bool:
        text = str(content or "")
        search_from = 0
        while True:
            start_idx = text.find(_ANIM_DRIVER_SECTION_MARKER_START, search_from)
            if start_idx == -1:
                return False
            end_idx = text.find(
                _ANIM_DRIVER_SECTION_MARKER_END,
                start_idx + len(_ANIM_DRIVER_SECTION_MARKER_START),
            )
            if end_idx == -1:
                return True
            search_from = end_idx + len(_ANIM_DRIVER_SECTION_MARKER_END)

    @staticmethod
    def _strip_existing_anim_driver_section(content: str):
        removed = False
        remaining = str(content or "")
        while True:
            start_idx = remaining.find(_ANIM_DRIVER_SECTION_MARKER_START)
            if start_idx == -1:
                return remaining, removed
            end_idx = remaining.find(_ANIM_DRIVER_SECTION_MARKER_END, start_idx)
            if end_idx == -1:
                return remaining, removed
            end_idx += len(_ANIM_DRIVER_SECTION_MARKER_END)
            remaining = remaining[:start_idx] + remaining[end_idx:]
            removed = True

    def _collect_ini_paragraphs(self, blueprint):
        # 在 execute 上下文中修正所有驱动节点的 auto_index
        for node in blueprint.nodes:
            if hasattr(node, '_ensure_valid_index') and callable(node._ensure_valid_index):
                try:
                    node._ensure_valid_index()
                except Exception:
                    pass

        collector = AnimationDriverCollector(blueprint)
        paragraphs = collector.collect()
        summary = getattr(collector, "last_normalization", None) or {}
        merges = summary.get("merges") or []
        renames = summary.get("renames") or []
        if merges or renames:
            print(
                f"[AnimDriver] 驱动块重复变量归一：合并同值声明 {len(merges)} 条，"
                f"异值分叉 {len(renames)} 个"
            )
        return paragraphs
    def _compose_updated_ini_content(self, original_content: str, ini_content: str):
        content_without_section, _removed = self._strip_existing_anim_driver_section(original_content)
        base_content, tail_content = SSMTNode_PostProcess_Base.split_auto_appended_tail_content(content_without_section)

        content_parts = []
        stripped_ini = str(ini_content or "").strip()
        stripped_base = str(base_content or "").strip()
        stripped_tail = str(tail_content or "").strip()

        if stripped_ini:
            content_parts.append(stripped_ini)
        if stripped_base:
            content_parts.append(stripped_base)

        merged_content = "\n\n".join(content_parts)
        if stripped_tail:
            if merged_content:
                merged_content = f"{merged_content}\n\n{stripped_tail}"
            else:
                merged_content = stripped_tail

        return merged_content

    @staticmethod
    def _find_target_ini_file(mod_export_path):
        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return "", "导出目录中未找到任何 .ini 文件"

        workspace_name = str(GlobalConfig.get_workspace_name() or "").strip()
        if workspace_name:
            exact_candidates = [
                path for path in ini_files
                if os.path.splitext(os.path.basename(path))[0] == workspace_name
            ]
            if len(exact_candidates) == 1:
                return exact_candidates[0], ""

            prefixed_candidates = [
                path for path in ini_files
                if os.path.basename(path).startswith(f"{workspace_name}_")
            ]
            all_workspace_candidates = exact_candidates + [
                path for path in prefixed_candidates if path not in exact_candidates
            ]
            if len(all_workspace_candidates) == 1:
                return all_workspace_candidates[0], ""
            if len(all_workspace_candidates) > 1:
                candidate_names = ", ".join(sorted(os.path.basename(path) for path in all_workspace_candidates))
                return "", f"导出目录中存在多个匹配当前工作空间的 INI 文件，请先清理或仅保留目标文件: {candidate_names}"

        if len(ini_files) == 1:
            return ini_files[0], ""

        candidate_names = ", ".join(sorted(os.path.basename(path) for path in ini_files))
        return "", f"导出目录中存在多个 INI 文件，无法确定应刷新哪一个: {candidate_names}"

    def refresh_exported_anim_driver_section(self, mod_export_path):
        blueprint, error_message = self._get_anim_driver_blueprint()
        if blueprint is None:
            return False, error_message

        paragraphs = self._collect_ini_paragraphs(blueprint)
        ini_content = self._build_ini_content(paragraphs) if paragraphs else ""

        target_ini_file, target_ini_error = self._find_target_ini_file(mod_export_path)
        if not target_ini_file:
            return False, target_ini_error

        try:
            with open(target_ini_file, 'r', encoding='utf-8') as f:
                original_content = f.read()
        except Exception as e:
            return False, f"读取目标INI文件失败: {e}"

        if self._has_incomplete_anim_driver_section(original_content):
            return False, "检测到旧的动画驱动段缺少结束标记，已停止刷新以避免误删其他配置；请手动删除该段后重试"

        new_content = self._compose_updated_ini_content(original_content, ini_content)
        if new_content == original_content:
            if paragraphs:
                return True, f"动画驱动段已是最新状态: {os.path.basename(target_ini_file)}"
            return True, f"未检测到动画驱动段变更: {os.path.basename(target_ini_file)}"

        self._create_cumulative_backup(target_ini_file, mod_export_path)

        try:
            with open(target_ini_file, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except Exception as e:
            return False, f"写入INI文件失败: {e}"

        if paragraphs:
            return True, f"已刷新 {os.path.basename(target_ini_file)} 中的动画驱动段，共 {len(paragraphs)} 个段落"
        return True, f"未找到动画驱动节点，已清除 {os.path.basename(target_ini_file)} 中旧的动画驱动段"

    def execute_postprocess(self, mod_export_path):
        print(f"动画驱动蓝图后处理节点开始执行，Mod导出路径: {mod_export_path}")
        success, message = self.refresh_exported_anim_driver_section(mod_export_path)
        if success:
            print(message)
        else:
            raise RuntimeError(message)

    @staticmethod
    def _anim_driver_active_flag() -> str:
        """角色激活标志（`$active0`，NTEMI 为 `$ntmi_active0`）。

        与动画驱动开关节点 KeyToggle 的 `condition` 同源：它在角色被绘制的那一帧
        由 VB/IB override 置 1，帧尾由主 [Present] 的 `post $active0 = 0` 清零。
        动画驱动段整体插在配置表最上方，所以这里读到的就是**本帧**的在场状态。
        """
        return SSMTNode_AnimDriver_Base._get_activation_flag()

    def _gate_present_content(self, para_content: str) -> str:
        """把单个段落的 ``[Present]`` 主体包进角色门控。

        动画驱动段的每帧算术（帧计数、索引/往返/随机/触发器）只有在角色**在场**时
        才有意义：不在场时它算出来的东西没人消费，却要在每个模组的每帧都跑一遍
        （本模组实测 466 条语句/帧）。装几十上百个同类模组时这是纯 CPU 空转，
        所以整段 `[Present]` 由 `if $active0 == 1` 门控。

        只包 `[Present]` **段本身**：段落里 `[Present]` 之后还可能跟着别的段
        （开关节点发的是 `[KeyToggle_*]`，其段位置取决于节点的拓扑/名称排序），
        段头一旦被裹进 `if` 里就是非法结构，整段热键会失效。`[Constants]` /
        `[KeySwap_*]` 同样不受影响 —— 热键输入照常，角色回到场上时驱动继续推进。
        """
        content = str(para_content or "")
        lines = content.split("\n")
        present_index = None
        for index, line in enumerate(lines):
            if line.strip() == "[Present]":
                present_index = index
                break
        if present_index is None:
            return content

        # [Present] 段的结束 = 下一个段头（没有则到内容末尾）
        body_end = len(lines)
        for index in range(present_index + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                body_end = index
                break

        body = lines[present_index + 1:body_end]
        if not any(line.strip() for line in body):
            return content

        guard = self._anim_driver_active_flag()
        return "\n".join(
            lines[:present_index + 1]
            + [f"if {guard} == 1"]
            + body
            + ["endif"]
            + lines[body_end:]
        )

    def _build_ini_content(self, paragraphs) -> str:
        lines = [
            _ANIM_DRIVER_SECTION_MARKER_START,
            "; ==============================================================================",
            "; 动画驱动配置 (自动生成)",
            "; ==============================================================================",
            "",
        ]

        for paragraph in paragraphs:
            para_content = mark_emitter_gate_writes(paragraph["ini_content"], include_local=False)
            if para_content:
                lines.append(self._gate_present_content(para_content))
                lines.append("")
                lines.append("; ------------------------------------------------------------------------------")
                lines.append("")

        lines.append(_ANIM_DRIVER_SECTION_MARKER_END)
        return "\n".join(lines)

    def _create_cumulative_backup(self, ini_file_path, mod_export_path):
        try:
            if not os.path.exists(ini_file_path):
                print(f"文件不存在，跳过备份: {ini_file_path}")
                return

            backup_dir = os.path.join(mod_export_path, "Backups")
            os.makedirs(backup_dir, exist_ok=True)

            base_filename = os.path.basename(ini_file_path)
            timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_filename = f"{base_filename}.{timestamp}.bak"
            backup_path = os.path.join(backup_dir, backup_filename)

            shutil.copy2(ini_file_path, backup_path)
            print(f"已创建备份: {backup_path}")
        except Exception as e:
            print(f"创建备份失败: {e}")


classes = (
    SSMT_OT_CreateAnimDriverBlueprint,
    SSMT_OT_RefreshAnimDriverExportSection,
    SSMTNode_PostProcess_AnimDriver,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
