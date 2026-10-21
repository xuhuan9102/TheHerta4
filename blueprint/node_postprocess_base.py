import bpy
import os
import shutil
import datetime
from bpy.types import Node, NodeSocket

from .node_base import SSMTNodeBase


class SSMTNode_PostProcess_Base(SSMTNodeBase):
    bl_icon = 'FILE_REFRESH'
    bl_width_min = 300
    AUTO_APPENDED_SECTION_MARKERS = (
        "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
        "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
        "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
    )
    AUTO_APPENDED_SECTION_MARKER_PREFIXES = (
        "; --- AUTO-APPENDED UI PANEL ",
        # 文本追加节点（node_postprocess_text_append）写在配置表最下方的块。
        "; --- AUTO-APPENDED CUSTOM TEXT ",
        # 跳过节点（node_postprocess_skip）追加的 IBSkip 块。
        "; --- AUTO-APPENDED SKIP ",
    )

    ANIM_DRIVER_SECTION_MARKER_START = "; --- ANIMATION DRIVER SECTION ---"
    ANIM_DRIVER_SECTION_MARKER_END = "; --- END ANIMATION DRIVER SECTION ---"

    def init(self, context):
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.outputs.new('SSMTSocketPostProcess', "Output")
        self.width = 300

    def execute_postprocess(self, mod_export_path):
        raise NotImplementedError("子类必须实现 execute_postprocess 方法")

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

    @classmethod
    def split_auto_appended_tail_content(cls, content: str):
        """Recognize all auto-appended markers, including dynamic UI panel markers."""
        text = str(content or "")
        offset = 0
        for line in text.splitlines(keepends=True):
            if cls.is_known_auto_appended_marker(line):
                return text[:offset], text[offset:]
            offset += len(line)
        return text, ""

    @classmethod
    def is_known_auto_appended_marker(cls, line: str) -> bool:
        stripped = str(line or "").strip()
        if stripped in cls.AUTO_APPENDED_SECTION_MARKERS:
            return True
        return any(stripped.startswith(prefix) for prefix in cls.AUTO_APPENDED_SECTION_MARKER_PREFIXES)

    @classmethod
    def split_anim_driver_block_content(cls, content: str):
        # Extract a complete animation-driver block from the top of an INI file.
        # Other postprocess nodes must not parse its [Constants]/[Present]
        # together with the main body, otherwise duplicate sections get merged.
        text = str(content or "")
        lines = text.splitlines(keepends=True)
        start_index = next(
            (index for index, line in enumerate(lines)
             if cls.ANIM_DRIVER_SECTION_MARKER_START in line),
            None,
        )
        if start_index is None:
            return "", text

        end_index = next(
            (index for index in range(start_index + 1, len(lines))
             if cls.ANIM_DRIVER_SECTION_MARKER_END in lines[index]),
            None,
        )
        if end_index is None:
            # Incomplete blocks are risky to isolate; keep them in the body so
            # the animation-driver refresh safety check can handle them.
            return "", text

        driver_content = "".join(lines[start_index:end_index + 1])
        before_content = "".join(lines[:start_index]).rstrip("\r\n")
        after_content = "".join(lines[end_index + 1:]).lstrip("\r\n")
        remaining_content = "\n\n".join(
            part for part in (before_content, after_content) if part
        )
        return driver_content, remaining_content


def register():
    pass


def unregister():
    pass
