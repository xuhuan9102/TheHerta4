import bpy
import glob
import os
import re

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_material import SSMTNode_PostProcess_MaterialBase


class SSMTNode_PostProcess_CommentCleanup(SSMTNode_PostProcess_Base):
    bl_idname = "SSMTNode_PostProcess_CommentCleanup"
    bl_label = "配置文件清理"
    bl_description = "导出时将生成的 INI 文件中的中文和其他非 ASCII 文本转换为英文标识"

    def draw_buttons(self, context, layout):
        layout.label(text="将INI中的中文和非ASCII文本转换为英文标识", icon="TEXT")

    def execute_postprocess(self, mod_export_path):
        changed_files = 0
        replaced_chars = 0
        for ini_path in glob.glob(os.path.join(mod_export_path, "**", "*.ini"), recursive=True):
            try:
                with open(ini_path, "r", encoding="utf-8-sig", newline="") as handle:
                    content = handle.read()
                cleaned = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(content)
                if cleaned == content:
                    continue
                with open(ini_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(cleaned)
                changed_files += 1
                replaced_chars += sum(1 for char in content if ord(char) > 127)
            except (OSError, UnicodeError) as exc:
                print(f"配置注释清理读取/写入失败 {ini_path}: {exc}")
        print(f"配置文件清理完成：处理 {changed_files} 个INI文件，替换 {replaced_chars} 个非ASCII字符。")


classes = (SSMTNode_PostProcess_CommentCleanup,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
