import bpy
import os
import glob
import hashlib
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base


class SSMTNode_PostProcess_ResourceMerge(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_ResourceMerge'
    bl_label = '资源合并'
    bl_description = '通过计算贴图文件内容的MD5哈希值，自动合并内容相同的资源引用并删除重复的贴图文件'

    def draw_buttons(self, context, layout):
        layout.label(text="计算贴图文件MD5哈希值", icon='FILE_CACHE')
        layout.label(text="合并内容相同的资源引用")
        layout.label(text="自动删除重复的贴图文件")
        layout.separator()
        layout.label(text="执行前会自动备份ini文件", icon='BACK')

    def compute_file_hash(self, file_path, block_size=65536):
        if not os.path.exists(file_path):
            return None
        hasher = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                while True:
                    data = f.read(block_size)
                    if not data:
                        break
                    hasher.update(data)
            return hasher.hexdigest()
        except (OSError, IOError):
            return None

    def execute_postprocess(self, mod_export_path):
        print(f"[ResourceMerge] 开始执行，Mod导出路径: {mod_export_path}")
        print(f"[ResourceMerge] 路径是否存在: {os.path.exists(mod_export_path)}")

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        print(f"[ResourceMerge] 找到 {len(ini_files)} 个ini文件: {ini_files}")
        if not ini_files:
            print("[ResourceMerge] 在路径中未找到任何.ini文件，跳过")
            return

        for ini_file in ini_files:
            self.process_ini_file(ini_file, mod_export_path)

        print("[ResourceMerge] 资源引用合并完成！")

    def process_ini_file(self, ini_file, mod_export_path):
        print(f"[ResourceMerge] 正在处理ini文件: {ini_file}")
        self._create_cumulative_backup(ini_file, mod_export_path)

        with open(ini_file, 'r', encoding='utf-8') as f:
            content = f.read()

        preserved_tail_content = ""
        content, preserved_tail_content = self.split_auto_appended_tail_content(content)
        if preserved_tail_content:
            print("[ResourceMerge] 检测到自动追加尾块，将保留")

        lines = content.splitlines()

        sections = OrderedDict()
        current_section = None

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                current_section = stripped
                sections[current_section] = []
            elif current_section:
                sections[current_section].append(line)

        resource_sections = {k: v for k, v in sections.items() if k.startswith('[Resource-')}
        print(f"[ResourceMerge] ini中共有 {len(sections)} 个section，其中 {len(resource_sections)} 个Resource section")

        file_hash_to_first_ref = {}
        files_to_delete = set()

        for section_name, section_lines in resource_sections.items():
            filename = next(
                (l.split('=', 1)[1].strip() for l in section_lines if l.strip().startswith('filename =')),
                None
            )
            if not filename:
                print(f"[ResourceMerge] 跳过 {section_name}: 未找到filename")
                continue

            file_path = os.path.join(mod_export_path, filename.replace("/", os.sep))
            if not os.path.exists(file_path):
                print(f"[ResourceMerge] 跳过 {section_name}: 文件不存在 {file_path}")
                continue

            file_hash = self.compute_file_hash(file_path)
            if not file_hash:
                print(f"[ResourceMerge] 跳过 {section_name}: 无法计算哈希")
                continue

            print(f"[ResourceMerge] {section_name} -> {filename} (MD5: {file_hash[:16]}...)")

            if file_hash in file_hash_to_first_ref:
                files_to_delete.add(file_path)
                print(f"[ResourceMerge]   重复! 与 {file_hash_to_first_ref[file_hash]['section']} 相同")
            else:
                file_hash_to_first_ref[file_hash] = {
                    'section': section_name,
                    'filename': filename
                }

        print(f"[ResourceMerge] 扫描完成: {len(file_hash_to_first_ref)} 个唯一资源, {len(files_to_delete)} 个重复文件待删除")

        modified = False
        for section_name, section_lines in resource_sections.items():
            for i, line in enumerate(section_lines):
                if line.strip().startswith('filename ='):
                    original_filename = line.split('=', 1)[1].strip()
                    file_path = os.path.join(mod_export_path, original_filename.replace("/", os.sep))

                    if os.path.exists(file_path):
                        file_hash = self.compute_file_hash(file_path)
                        if file_hash and file_hash in file_hash_to_first_ref:
                            primary_filename = file_hash_to_first_ref[file_hash]['filename']
                            if original_filename != primary_filename:
                                section_lines[i] = f"filename = {primary_filename}"
                                modified = True
                                print(f"[ResourceMerge] 引用替换: {original_filename} -> {primary_filename}")
                    break

        if modified:
            print(f"[ResourceMerge] ini文件已修改，正在写入...")
            new_content = []
            for section_name, lines in sections.items():
                new_content.append(section_name)
                new_content.extend(lines)
                new_content.append('')

            if preserved_tail_content:
                new_content.append('')
                new_content.append(preserved_tail_content)

            with open(ini_file, 'w', encoding='utf-8') as f:
                f.write("\n".join(new_content))
        else:
            print(f"[ResourceMerge] ini文件无需修改")

        for file_path in files_to_delete:
            try:
                os.remove(file_path)
                print(f"[ResourceMerge] 已删除重复文件: {os.path.relpath(file_path, mod_export_path)}")
            except OSError as e:
                print(f"[ResourceMerge] 删除文件失败 {file_path}: {e}")


classes = (
    SSMTNode_PostProcess_ResourceMerge,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
