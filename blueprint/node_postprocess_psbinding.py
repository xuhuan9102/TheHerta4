import bpy
import glob
import os
import shutil
import re
from collections import OrderedDict
from pathlib import Path

from ..toolkit.tt_dds_conversion import (
    convert_texture_to_dds,
    existing_dds_matches_format,
    resolve_texture_dds_format,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base


class PSBindingItem(bpy.types.PropertyGroup):
    slot: bpy.props.IntProperty(
        name="PS槽位",
        description="像素着色器贴图槽位 (0-7)",
        default=0,
        min=0,
        max=7,
    )
    resource_suffix: bpy.props.StringProperty(
        name="资源名后缀",
        description="自动添加 Resource_ 前缀",
        default=""
    )
    texture_filename: bpy.props.StringProperty(
        name="贴图文件",
        subtype='FILE_PATH',
        default=""
    )
    auto_copy: bpy.props.BoolProperty(
        name="自动复制",
        default=True
    )


class SSMT_OT_PSBindingAddItem(bpy.types.Operator):
    bl_idname = "ssmt.psbinding_add_item"
    bl_label = "添加贴图绑定"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_PSBinding':
            # 自动递增槽位：新绑定的槽位为当前列表长度
            new_slot = len(node.ps_bindings)
            if new_slot > 7:
                self.report({'WARNING'}, "最多支持8个槽位 (0-7)，已达到上限")
                return {'CANCELLED'}
            item = node.ps_bindings.add()
            item.slot = new_slot
            item.resource_suffix = f"Binding_{new_slot}"
        return {'FINISHED'}


class SSMT_OT_PSBindingRemoveItem(bpy.types.Operator):
    bl_idname = "ssmt.psbinding_remove_item"
    bl_label = "删除贴图绑定"
    bl_options = {'REGISTER', 'INTERNAL'}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_PSBinding':
            if 0 <= self.item_index < len(node.ps_bindings):
                node.ps_bindings.remove(self.item_index)
                # 删除后重新整理序号，使列表连续
                for idx, bind in enumerate(node.ps_bindings):
                    bind.slot = idx
        return {'FINISHED'}


class SSMTNode_PostProcess_PSBinding(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_PSBinding'
    bl_label = 'PS绑定 + IB限定'
    bl_description = '为指定IB哈希的第一个有效 TextureOverride 块添加 ps-tX 绑定（跳过 handling=skip 的 stub 块）'

    ib_hash: bpy.props.StringProperty(name="IB哈希", default="")
    ps_hash: bpy.props.StringProperty(name="PS哈希", default="")
    shader_suffix: bpy.props.StringProperty(
        name="ShaderOverride后缀",
        description="自动添加 ShaderOverride_ 前缀",
        default=""
    )
    ps_bindings: bpy.props.CollectionProperty(type=PSBindingItem)
    active_binding_index: bpy.props.IntProperty(default=0)
    export_textures_as_dds: bpy.props.BoolProperty(
        name="导出时把贴图转成DDS",
        description=(
            "导出 Mod 时，绑定的贴图若不是 .dds（例如编辑期用的无损 PNG），"
            "先用 texconv 按现有 DDS 转换规则转成 DDS 放进 Mod 的 Textures 目录，"
            "INI 的 filename 也指向转出来的 DDS。源贴图文件保留不动。"
            "关闭时按原样复制源文件"
        ),
        default=True,
    )

    def draw_buttons(self, context, layout):
        layout.prop(self, "ib_hash")
        layout.prop(self, "ps_hash")
        layout.prop(self, "shader_suffix")

        box = layout.box()
        box.label(text="PS贴图绑定")
        if self.ps_bindings:
            for i, bind in enumerate(self.ps_bindings):
                row = box.row(align=True)
                # 槽位显示为只读文本（因为已经自动递增）
                row.label(text=f"ps-t{bind.slot}")
                row.prop(bind, "resource_suffix", text="资源名后缀")
                row.prop(bind, "texture_filename", text="文件")
                row.prop(bind, "auto_copy", text="", icon='COPY_ID')
                op = row.operator("ssmt.psbinding_remove_item", text="", icon='X')
                op.node_name = self.name
                op.item_index = i
        else:
            box.label(text="未添加绑定", icon='INFO')
        row = box.row(align=True)
        op = row.operator("ssmt.psbinding_add_item", text="添加绑定", icon='ADD')
        op.node_name = self.name
        box.prop(self, "export_textures_as_dds")

    # ---------- INI 读写 ----------
    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        preserved_tail_content = ""
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    if current_section not in sections:
                        sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line)
        except FileNotFoundError:
            return None, ""
        return sections, preserved_tail_content

    def _merge_duplicate_sections(self, sections):
        merged = OrderedDict()
        lower_to_original = {}
        for sec_name, lines in sections.items():
            lower_name = sec_name.lower()
            if lower_name in lower_to_original:
                original_name = lower_to_original[lower_name]
                existing_lines = merged[original_name]
                for line in lines:
                    if line not in existing_lines:
                        existing_lines.append(line)
            else:
                lower_to_original[lower_name] = sec_name
                merged[sec_name] = lines[:]
        return merged

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content=""):
        sections = self._merge_duplicate_sections(sections)
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            section_items = list(sections.items())
            for idx, (sec_name, lines) in enumerate(section_items):
                f.write(f"{sec_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                if idx < len(section_items) - 1:
                    f.write("\n")
            if preserved_tail_content:
                f.write("\n" + preserved_tail_content)

    # ---------- 有效性检查 ----------
    def _is_valid_draw_section(self, lines):
        handling_skip_idx = -1
        for idx, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower() == 'handling = skip':
                handling_skip_idx = idx
                break
        if handling_skip_idx == -1:
            return True
        for line in lines[handling_skip_idx + 1:]:
            stripped = line.strip()
            if not stripped or stripped.startswith(';'):
                continue
            return True
        return False

    # ---------- 查找第一个有效块 ----------
    def _find_first_valid_texture_override_section_by_hash(self, sections, target_hash):
        for sec_name, lines in sections.items():
            if not sec_name.lower().startswith('[textureoverride_'):
                continue
            has_hash = False
            for line in lines:
                stripped = line.strip()
                if stripped.lower().startswith('hash ='):
                    parts = stripped.split('=', 1)
                    if len(parts) == 2 and parts[1].strip() == target_hash:
                        has_hash = True
                        break
            if not has_hash:
                continue
            if self._is_valid_draw_section(lines):
                return sec_name, lines
        return None, None

    # ---------- 插入位置 ----------
    def _find_insert_position(self, lines):
        ib_re = re.compile(r'^\s*ib\s*=', re.I)
        match_first_re = re.compile(r'^\s*match_first_index\s*=', re.I)
        handling_skip_re = re.compile(r'^\s*handling\s*=\s*skip\s*$', re.I)
        hash_re = re.compile(r'^\s*hash\s*=', re.I)

        for idx, line in enumerate(lines):
            if ib_re.match(line.strip()):
                return idx + 1
        for idx, line in enumerate(lines):
            if match_first_re.match(line.strip()):
                return idx + 1
        for idx, line in enumerate(lines):
            if handling_skip_re.match(line.strip()):
                return idx + 1
        for idx, line in enumerate(lines):
            if hash_re.match(line.strip()):
                return idx + 1
        return 1

    def _insert_ps_bindings_into_section(self, lines, bindings):
        ps_t_re = re.compile(r'^\s*ps-t(\d+)\s*=', re.I)
        existing_slots = set()
        for line in lines:
            m = ps_t_re.match(line.strip())
            if m:
                existing_slots.add(int(m.group(1)))

        new_lines = []
        for slot_str, resource in bindings:
            slot = int(slot_str.replace('ps-t', ''))
            if slot in existing_slots:
                continue
            new_lines.append(f"{slot_str} = {resource}")
            existing_slots.add(slot)

        if not new_lines:
            return False

        insert_pos = self._find_insert_position(lines)
        for new_line in reversed(new_lines):
            lines.insert(insert_pos, new_line)
        return True

    # ---------- 创建块（避免重复） ----------
    def _ensure_shader_override_section(self, sections, suffix, ps_hash):
        if not suffix:
            suffix = f"{ps_hash[:12]}"
        full_name = f"ShaderOverride_{suffix}"
        sec_name = f"[{full_name}]"
        lower_sec_name = sec_name.lower()
        existing_sec_name = None
        for existing in sections.keys():
            if existing.lower() == lower_sec_name:
                existing_sec_name = existing
                break
        if existing_sec_name:
            lines = sections[existing_sec_name]
            has_hash = any(line.strip().lower().startswith('hash =') for line in lines)
            if not has_hash:
                lines.insert(1, f"hash = {ps_hash}")
            has_check = any('checktextureoverride' in line.lower() for line in lines)
            if not has_check:
                lines.append("checktextureoverride = ib")
        else:
            # 修正：内容行列表不应包含节头，只包含键值对
            sections[sec_name] = [f"hash = {ps_hash}", "checktextureoverride = ib"]

    def _ensure_resource_section(self, sections, suffix, filename):
        full_name = f"Resource_{suffix}"
        sec_name = f"[{full_name}]"
        lower_sec_name = sec_name.lower()
        existing_sec_name = None
        for existing in sections.keys():
            if existing.lower() == lower_sec_name:
                existing_sec_name = existing
                break
        if existing_sec_name:
            lines = sections[existing_sec_name]
            has_filename = any(line.strip().lower().startswith('filename =') for line in lines)
            if not has_filename:
                lines.append(f"filename = {filename}")
        else:
            # 修正：内容行列表不应包含节头，只包含键值对
            sections[sec_name] = [f"filename = {filename}"]

    # ---------- 复制贴图 ----------
    @staticmethod
    def _texture_type_from_suffix(suffix):
        """从资源名后缀里提取已知贴图类型；提取不到返回空串（走文件名兜底）。"""
        lowered = str(suffix or "").lower()
        for name in ("DiffuseMap", "NormalMap", "LightMap", "MaterialMap"):
            if name.lower() in lowered:
                return name
        return ""

    def _copy_texture_file(self, src_path, dest_dir, texture_type="", name_hints=()):
        src = Path(src_path)
        if not src.is_file():
            print(f"文件不存在: {src_path}")
            return None
        textures_dir = dest_dir / "Textures"
        textures_dir.mkdir(parents=True, exist_ok=True)

        # 导出 DDS 优先：编辑期贴图常是无损 PNG（避免反复存 DDS 被过度压缩），但游戏只认
        # DDS，所以导出时按现有 DDS 规则转一份放进 Mod。INI 的 filename 取本函数返回值，
        # 因此自然跟着指向 .dds。转换失败回退为原样复制，不中断导出。
        if getattr(self, "export_textures_as_dds", False) and src.suffix.lower() != ".dds":
            converted = textures_dir / f"{src.stem}.dds"
            # 「已存在 DDS」不能盲信：格式与当前规则期望不一致时必须重转，
            # 否则规则改对后旧线性 DDS 会被永远沿用。
            needs_conversion = not converted.exists()
            if not needs_conversion:
                expected_format = resolve_texture_dds_format(
                    str(src),
                    None,
                    texture_type=texture_type,
                    name_hints=name_hints,
                )
                needs_conversion = not existing_dds_matches_format(
                    str(converted), expected_format
                )
            if needs_conversion:
                ok, reason = convert_texture_to_dds(
                    str(src),
                    str(converted),
                    texture_type=texture_type,
                    name_hints=name_hints,
                )
                if not ok:
                    print(f"贴图转 DDS 失败，回退为原样复制: {src_path} - {reason}")
                    converted = None
            if converted is not None and converted.exists():
                return f"Textures/{converted.name}"

        dest_path = textures_dir / src.name
        if dest_path.exists():
            if src.stat().st_mtime <= dest_path.stat().st_mtime:
                return f"Textures/{src.name}"
        try:
            shutil.copy2(src, dest_path)
            print(f"已复制: {src_path} -> {dest_path}")
            return f"Textures/{src.name}"
        except Exception as e:
            print(f"复制失败: {e}")
            return None

    # ---------- 主执行 ----------
    def execute_postprocess(self, mod_export_path):
        print(f"PS绑定+IB限定后处理节点开始执行，Mod导出路径: {mod_export_path}")

        if not self.ib_hash or not self.ps_hash:
            print("错误: 未填写 IB哈希 或 PS哈希")
            return

        output_root = Path(mod_export_path)
        textures_dir = output_root / "Textures"
        textures_dir.mkdir(parents=True, exist_ok=True)

        valid_bindings = []
        for bind in self.ps_bindings:
            suffix = bind.resource_suffix.strip()
            if not suffix:
                print(f"跳过: 槽位 {bind.slot} 资源名后缀为空")
                continue
            tex_file = bind.texture_filename.strip()
            if not tex_file:
                print(f"跳过: 槽位 {bind.slot} 贴图文件为空")
                continue

            final_filename = None
            if bind.auto_copy:
                final_filename = self._copy_texture_file(
                    tex_file,
                    output_root,
                    texture_type=self._texture_type_from_suffix(suffix),
                    name_hints=(suffix,),
                )
                if final_filename is None:
                    final_filename = tex_file
            else:
                final_filename = tex_file

            valid_bindings.append((bind.slot, suffix, final_filename))

        if not valid_bindings:
            print("没有有效的贴图绑定")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("未找到 .ini 文件")
            return
        target_ini = ini_files[0]

        self._create_cumulative_backup(target_ini, mod_export_path)
        sections, tail = self._read_ini_to_ordered_dict(target_ini)
        if sections is None:
            print("无法读取 INI 文件")
            return

        target_sec_name, target_lines = self._find_first_valid_texture_override_section_by_hash(sections, self.ib_hash)
        if target_sec_name is None:
            print(f"未找到有效的（非 stub）hash={self.ib_hash} 的 TextureOverride 块")
            return

        # 构建绑定列表：使用实际槽位
        bindings_for_sec = [(f"ps-t{bind[0]}", f"Resource_{bind[1]}") for bind in valid_bindings]
        if self._insert_ps_bindings_into_section(target_lines, bindings_for_sec):
            print(f"已修改: {target_sec_name}")

        self._ensure_shader_override_section(sections, self.shader_suffix.strip(), self.ps_hash)
        for slot, suffix, filename in valid_bindings:
            self._ensure_resource_section(sections, suffix, filename)

        self._write_ordered_dict_to_ini(sections, target_ini, tail)
        print("PS绑定+IB限定后处理节点执行完成")


classes = (
    PSBindingItem,
    SSMT_OT_PSBindingAddItem,
    SSMT_OT_PSBindingRemoveItem,
    SSMTNode_PostProcess_PSBinding,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)