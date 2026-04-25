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
        "; --- AUTO-APPENDED WEB PANEL PRESET START ---",
    )

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
        first_marker_position = None

        for marker in cls.AUTO_APPENDED_SECTION_MARKERS:
            marker_position = content.find(marker)
            if marker_position == -1:
                continue

            if first_marker_position is None or marker_position < first_marker_position:
                first_marker_position = marker_position

        if first_marker_position is None:
            return content, ""

        return content[:first_marker_position], content[first_marker_position:]


def register():
    pass


def unregister():
    pass
