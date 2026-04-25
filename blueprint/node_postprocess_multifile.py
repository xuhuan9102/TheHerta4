import bpy
import os
import glob
import re
import shutil
from collections import OrderedDict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from .node_postprocess_base import SSMTNode_PostProcess_Base


class SSMTNode_PostProcess_MultiFile(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_MultiFile'
    bl_label = '多文件配置'
    bl_description = '为指定哈希值的物体生成多文件动画配置，支持紧凑缓冲区和顶点增量存储'

    hash_values: bpy.props.StringProperty(
        name="哈希值",
        description="需要处理的哈希值，多个用逗号分隔。支持两种格式：\n1. IB hash（如：bb0999e6）\n2. 完整名称（如：bb0999e6-43191-0）\n会自动提取IB hash进行查找",
        default="",
        update=lambda self, context: self.update_node_width([self.hash_values, self.animation_swapkey, self.active_swapkey, self.comment])
    )
    animation_swapkey: bpy.props.StringProperty(
        name="循环参数名",
        description="用于动画帧切换的参数名称",
        default="$swapkey100",
        update=lambda self, context: self.update_node_width([self.hash_values, self.animation_swapkey, self.active_swapkey, self.comment])
    )
    active_swapkey: bpy.props.StringProperty(
        name="激活参数名",
        description="用于控制动画执行的参数名称",
        default="$active0",
        update=lambda self, context: self.update_node_width([self.hash_values, self.animation_swapkey, self.active_swapkey, self.comment])
    )
    comment: bpy.props.StringProperty(
        name="备注",
        description="备注信息，会以注释形式生成到配置表中",
        default="",
        update=lambda self, context: self.update_node_width([self.hash_values, self.animation_swapkey, self.active_swapkey, self.comment])
    )
    active_value: bpy.props.IntProperty(
        name="激活参数值",
        description="激活参数的值",
        default=1,
        min=0,
        max=100
    )

    def _hash_to_resource_prefix(self, h):
        return h.replace('-', '_')

    def draw_buttons(self, context, layout):
        layout.prop(self, "hash_values")
        layout.prop(self, "animation_swapkey")
        layout.prop(self, "active_swapkey")
        layout.prop(self, "active_value")
        layout.prop(self, "comment", text="备注")

        if not NUMPY_AVAILABLE:
            layout.label(text="警告: 未安装numpy库，功能不可用", icon='ERROR')

    def _get_vertex_attrs_node(self):
        if not self.inputs[0].is_linked:
            return None

        source_node = self.inputs[0].links[0].from_node
        if source_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
            return source_node

        if source_node.inputs[0].is_linked:
            prev_node = source_node.inputs[0].links[0].from_node
            if prev_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
                return prev_node

        return None

    def _get_shader_source_path(self):
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            asset_source_dir = os.path.join(addon_dir, "Toolset")
            shader_source_path = os.path.join(asset_source_dir, "merge_anim_packed_delta.hlsl")
            return shader_source_path
        except Exception as e:
            print(f"获取着色器模板路径时出错: {e}")
            return None

    def _get_vertex_struct_definition(self):
        vertex_attrs_node = self._get_vertex_attrs_node()
        if vertex_attrs_node:
            return vertex_attrs_node.get_vertex_struct_definition()
        return None

    @staticmethod
    def parse_vertex_struct(struct_definition):
        if not struct_definition or not struct_definition.strip():
            return None

        TYPE_SIZES = {
            'float': 4, 'float2': 8, 'float3': 12, 'float4': 16,
            'int': 4, 'int2': 8, 'int3': 12, 'int4': 16,
            'uint': 4, 'uint2': 8, 'uint3': 12, 'uint4': 16,
            'half': 2, 'half2': 4, 'half3': 6, 'half4': 8,
            'double': 8, 'double2': 16, 'double3': 24, 'double4': 32,
        }

        total_bytes = 0
        total_floats = 0
        attributes = []
        unrecognized_types = set()

        lines = struct_definition.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('//') or line.startswith('/*') or line.startswith('*'):
                continue

            line = line.rstrip(';').strip()

            parts = line.split()
            if len(parts) >= 2:
                type_name = parts[0]
                var_name = parts[1].rstrip(';')

                if type_name in TYPE_SIZES:
                    byte_size = TYPE_SIZES[type_name]
                    total_bytes += byte_size
                    total_floats += byte_size // 4
                    attributes.append({'type': type_name, 'name': var_name, 'size': byte_size})
                elif type_name.lower() != 'struct' and not line.endswith('{') and not line.endswith('}'):
                    unrecognized_types.add(type_name)

        if unrecognized_types:
            print(f"警告: 发现未识别的顶点属性类型: {', '.join(unrecognized_types)}")

        if total_bytes == 0:
            return None

        return (total_bytes, total_floats, attributes)

    def _get_vertex_size(self):
        struct_definition = self._get_vertex_struct_definition()
        if struct_definition:
            parsed = self.parse_vertex_struct(struct_definition)
            if parsed:
                _, num_floats, _ = parsed
                return num_floats
        return 10

    def _update_shader_file(self, shader_path):
        try:
            with open(shader_path, 'r', encoding='utf-8') as f:
                content = f.read()

            vertex_struct = self._get_vertex_struct_definition()
            if vertex_struct:
                content = re.sub(r"struct VertexAttributes\s*\{[^}]*\};", vertex_struct, content, flags=re.DOTALL)

            with open(shader_path, 'w', encoding='utf-8') as f:
                f.write(content)

            return True
        except Exception as e:
            print(f"更新着色器文件失败: {e}")
            return False

    def _parse_hash_values(self, hash_str):
        hash_list = [h.strip() for h in hash_str.split(',') if h.strip()]

        ib_hashes = set()
        for hash_value in hash_list:
            if '-' in hash_value:
                ib_hash = hash_value.split('-')[0]
                ib_hashes.add(ib_hash)
            else:
                ib_hashes.add(hash_value)

        return sorted(list(ib_hashes))

    def _read_Meshes_file(self, Meshes_path):
        try:
            with open(Meshes_path, 'rb') as f:
                return np.fromfile(f, dtype=np.float32)
        except Exception as e:
            print(f"读取缓冲区文件失败: {Meshes_path}. 原因: {e}")
            return None

    def _write_Meshes_file(self, Meshes_data, Meshes_path):
        try:
            os.makedirs(os.path.dirname(Meshes_path), exist_ok=True)
            Meshes_data.tofile(Meshes_path)
            return True
        except Exception as e:
            print(f"写入缓冲区文件失败: {Meshes_path}. 原因: {e}")
            return False

    def _calculate_deltas(self, base_Meshes, target_Meshes):
        min_len = min(len(base_Meshes), len(target_Meshes))
        if len(base_Meshes) != len(target_Meshes):
            print(f"警告: 基准缓冲区({len(base_Meshes)})和目标缓冲区({len(target_Meshes)})大小不一致，将使用较小的长度({min_len})进行计算")
        return target_Meshes[:min_len] - base_Meshes[:min_len]

    def _create_packed_Meshess(self, base_Meshes, target_Meshes, use_delta=True):
        try:
            min_len = min(len(base_Meshes), len(target_Meshes))
            if len(base_Meshes) != len(target_Meshes):
                print(f"警告: 基准缓冲区({len(base_Meshes)})和目标缓冲区({len(target_Meshes)})大小不一致，将使用较小的长度({min_len})进行计算")
                base_Meshes = base_Meshes[:min_len]
                target_Meshes = target_Meshes[:min_len]

            if use_delta:
                deltas = self._calculate_deltas(base_Meshes, target_Meshes)
            else:
                deltas = target_Meshes.copy()

            vertex_size = self._get_vertex_size()
            changed_indices = []
            changed_values = []

            if len(deltas) % vertex_size != 0:
                print(f"缓冲区长度不是顶点大小的整数倍: {len(deltas)} % {vertex_size} != 0")
                adjusted_length = (len(deltas) // vertex_size) * vertex_size
                deltas = deltas[:adjusted_length]
                if use_delta:
                    target_Meshes = target_Meshes[:adjusted_length]

            for i in range(0, len(deltas), vertex_size):
                position_delta = deltas[i:i+3]
                if not np.allclose(position_delta, [0, 0, 0], atol=1e-6):
                    changed_indices.append(i // vertex_size)
                    if use_delta:
                        changed_values.extend(position_delta)
                    else:
                        changed_values.extend(target_Meshes[i:i+3])

            map_array = np.full(len(deltas) // vertex_size, -1, dtype=np.int32)
            for idx, vert_idx in enumerate(changed_indices):
                map_array[vert_idx] = idx

            position_deltas_array = np.array(changed_values, dtype=np.float32)

            print(f"创建紧凑缓冲区: {len(changed_indices)}个顶点变化，原始顶点数: {len(deltas) // vertex_size}，顶点大小: {vertex_size}个float")

            return map_array, position_deltas_array
        except Exception as e:
            print(f"创建紧凑缓冲区失败: {str(e)}")
            return np.array([], dtype=np.int32), np.array([], dtype=np.float32)

    def _get_vertex_count(self, ini_sections, hash_value):
        for section_name, lines in ini_sections.items():
            if section_name.startswith(f'[TextureOverride_{hash_value}_') and '_VertexLimitRaise' in section_name:
                for line in lines:
                    if line.strip().startswith('override_vertex_count ='):
                        try:
                            return int(line.split('=', 1)[1].strip())
                        except ValueError:
                            continue
        return None

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        preserved_tail_content = ""

        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            if preserved_tail_content:
                print("[MultiFile] 检测到自动追加尾块，将保留")

            for line in content.splitlines():
                stripped_line = line.strip()
                if stripped_line.startswith('[') and stripped_line.endswith(']'):
                    current_section = stripped_line
                    sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line)
        except FileNotFoundError:
            return None, ""
        return sections, preserved_tail_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content=""):
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write("\n")

            if preserved_tail_content:
                f.write("\n")
                f.write(preserved_tail_content)

    def execute_postprocess(self, mod_export_path):
        print(f"多文件配置后处理节点开始执行，Mod导出路径: {mod_export_path}")

        if not NUMPY_AVAILABLE:
            print("需要安装numpy库以使用紧凑缓冲区和增量存储功能")
            return

        hash_values = self._parse_hash_values(self.hash_values)
        if not hash_values:
            print("请至少输入一个有效的哈希值")
            return

        try:
            original_cwd = os.getcwd()
            os.chdir(mod_export_path)

            ini_files = glob.glob("*.ini")
            if not ini_files:
                print("在路径中未找到任何.ini文件")
                os.chdir(original_cwd)
                return

            for ini_file in ini_files:
                ini_file_path = os.path.join(mod_export_path, ini_file)
                self._create_cumulative_backup(ini_file_path, mod_export_path)
                sections, preserved_tail_content = self._read_ini_to_ordered_dict(ini_file_path)
                if not sections:
                    continue

                Meshes_folders = []
                for i in range(1, 1000):
                    Meshes_folder = os.path.join(mod_export_path, f"Meshes{i:02d}")
                    if os.path.exists(Meshes_folder):
                        Meshes_folders.append(f"Meshes{i:02d}")
                    else:
                        break

                if len(Meshes_folders) < 2:
                    print(f"至少需要 Meshes01 和 Meshes02 两个文件夹才能进行多文件配置")
                    continue

                for section_name in list(sections.keys()):
                    if section_name.startswith('[Resource') and section_name.endswith(']'):
                        resource_name = section_name[1:-1]

                        original_lines = sections[section_name].copy()
                        new_lines = []
                        for line in original_lines:
                            modified_line = line
                            for buf_folder in Meshes_folders[1:]:
                                old_path = f"filename = {buf_folder}/"
                                if old_path in line:
                                    modified_line = line.replace(old_path, "filename = Meshes01/")
                                    break
                            new_lines.append(modified_line)

                        if 'Position' in resource_name:
                            sections[section_name] = [f'[{resource_name}_1]'] + new_lines
                        else:
                            sections[section_name] = new_lines

                processed_base_names = []

                for hash_value in hash_values:
                    Meshes01_path = os.path.join(mod_export_path, "Meshes01")
                    if not os.path.exists(Meshes01_path):
                        print(f"Meshes01 文件夹不存在: {Meshes01_path}")
                        continue

                    position_files = []
                    try:
                        for filename in os.listdir(Meshes01_path):
                            if filename.startswith(hash_value) and filename.endswith("-Position.buf"):
                                position_files.append(filename)
                    except Exception as e:
                        print(f"读取 Meshes01 文件夹失败: {e}")
                        continue

                    if not position_files:
                        print(f"在 Meshes01 中未找到以 {hash_value} 开头的 Position 文件")
                        print(f"查找路径: {Meshes01_path}")
                        print(f"查找模式: {hash_value}*-Position.buf")
                        continue

                    base_position_file = position_files[0]
                    print(f"找到 {len(position_files)} 个匹配的 Position 文件，使用: {base_position_file}")

                    base_name = base_position_file.replace("-Position.buf", "")
                    hash_prefix = self._hash_to_resource_prefix(base_name)
                    print(f"使用基础名称: {base_name}，资源前缀: {hash_prefix}")

                    processed_base_names.append((base_name, hash_prefix))

                    base_Meshes_path = os.path.join("Meshes01", base_position_file)
                    base_Meshes_full_path = os.path.join(mod_export_path, base_Meshes_path)

                    base_Meshes = self._read_Meshes_file(base_Meshes_full_path)
                    if base_Meshes is None:
                        continue

                    processed_frames = []
                    for Meshes_folder in Meshes_folders[1:]:
                        Meshes_folder_path = os.path.join(mod_export_path, Meshes_folder)
                        if not os.path.exists(Meshes_folder_path):
                            continue

                        target_position_files = []
                        try:
                            for filename in os.listdir(Meshes_folder_path):
                                if filename.startswith(hash_value) and filename.endswith("-Position.buf"):
                                    target_position_files.append(filename)
                        except Exception as e:
                            print(f"读取 {Meshes_folder} 文件夹失败: {e}")
                            continue

                        if not target_position_files:
                            continue

                        target_position_file = target_position_files[0]
                        target_filename = os.path.join(Meshes_folder, target_position_file)
                        target_Meshes_full_path = os.path.join(mod_export_path, target_filename)

                        if os.path.exists(target_Meshes_full_path):
                            target_Meshes = self._read_Meshes_file(target_Meshes_full_path)
                            if target_Meshes is None:
                                continue

                            map_array, pos_deltas_array = self._create_packed_Meshess(
                                base_Meshes, target_Meshes, True
                            )

                            pos_output_path = os.path.join(mod_export_path, Meshes_folder, f"{base_name}-Position_packed_pos_delta.buf")
                            self._write_Meshes_file(pos_deltas_array, pos_output_path)

                            map_output_path = os.path.join(mod_export_path, Meshes_folder, f"{base_name}-Position_map.buf")
                            self._write_Meshes_file(map_array, map_output_path)

                            folder_num = int(Meshes_folder.replace("Meshes", ""))
                            pos_resource_section = f'[Resource_{hash_prefix}_Position{folder_num:02d}_packed_pos_delta]'
                            stride = 12

                            sections[pos_resource_section] = [
                                'type = Buffer',
                                f'stride = {stride}',
                                f'filename = {Meshes_folder}/{base_name}-Position_packed_pos_delta.buf'
                            ]

                            map_resource_section = f'[Resource_{hash_prefix}_Position{folder_num:02d}_Map]'
                            sections[map_resource_section] = [
                                'type = Buffer',
                                'stride = 4',
                                f'filename = {Meshes_folder}/{base_name}-Position_map.buf'
                            ]

                            processed_frames.append((folder_num, Meshes_folder))

                    if not processed_frames:
                        print(f"没有找到有效的目标帧文件，跳过哈希值: {hash_value}")
                        continue

                    shader_section = f'[CustomShader_{base_name}_1Anim]'
                    shader_lines = []

                    if self.comment:
                        shader_lines.append("; " + self.comment)
                        shader_lines.append("")

                    for state_index, (folder_num, Meshes_folder) in enumerate(processed_frames, 1):
                        shader_lines.append(f"if {self.animation_swapkey} == {state_index}")
                        shader_lines.append(f"      cs-t51 = copy Resource_{hash_prefix}_Position{folder_num:02d}_packed_pos_delta")
                        shader_lines.append(f"endif")

                    shader_lines.append("")

                    for state_index, (folder_num, Meshes_folder) in enumerate(processed_frames, 1):
                        shader_lines.append(f"if {self.animation_swapkey} == {state_index}")
                        shader_lines.append(f"      cs-t75 = copy Resource_{hash_prefix}_Position{folder_num:02d}_Map")
                        shader_lines.append(f"endif")

                    shader_lines.append("")
                    shader_lines.append("    cs = ./res/merge_anim_packed_delta.hlsl")
                    shader_lines.append(f"    cs-u5 = copy Resource{hash_prefix}Position_1")
                    shader_lines.append(f"    Resource{hash_prefix}Position = ref cs-u5")

                    shader_source_path = self._get_shader_source_path()
                    if shader_source_path and os.path.exists(shader_source_path):
                        dest_res_dir = os.path.join(mod_export_path, "res")
                        os.makedirs(dest_res_dir, exist_ok=True)
                        shader_dest_path = os.path.join(dest_res_dir, "merge_anim_packed_delta.hlsl")
                        shutil.copy2(shader_source_path, shader_dest_path)
                        self._update_shader_file(shader_dest_path)
                        print(f"已复制并更新着色器文件: merge_anim_packed_delta.hlsl")

                    vertex_count = self._get_vertex_count(sections, hash_value)
                    if not vertex_count:
                        try:
                            file_size = os.path.getsize(base_Meshes_full_path)
                            vertex_size_bytes = self._get_vertex_size() * 4
                            vertex_count = file_size // vertex_size_bytes
                            print(f"  [DEBUG] 从文件大小推断顶点数: file={os.path.basename(base_Meshes_full_path)}, size={file_size}, vertex_size={vertex_size_bytes}, count={vertex_count}")
                        except Exception as e:
                            print(f"  [WARNING] 无法推断顶点数: {e}")
                            vertex_count = 100000
                    if vertex_count == 0:
                        vertex_count = 100000
                    shader_lines.append(f"    Dispatch = {vertex_count}, 1, 1")

                    shader_lines.append("    cs-u5 = null")
                    shader_lines.append("    cs-t51 = null")
                    shader_lines.append("    cs-t75 = null")

                    sections[shader_section] = shader_lines

                constants_section = '[Constants]'
                constants_lines = sections.get(constants_section, [])

                animation_swapkey_defined = False
                active_swapkey_defined = False

                for line in constants_lines:
                    if self.animation_swapkey in line:
                        animation_swapkey_defined = True
                    if self.active_swapkey in line:
                        active_swapkey_defined = True

                if not animation_swapkey_defined:
                    constants_lines.append(f"global persist {self.animation_swapkey} = 0")
                if not active_swapkey_defined:
                    constants_lines.append(f"global persist {self.active_swapkey} = 0")

                for base_name, hash_prefix in processed_base_names:
                    post_copy_line = f"post Resource{hash_prefix}Position = copy_desc Resource{hash_prefix}Position_1"
                    post_run_line = f"post run = CustomShader_{base_name}_1Anim"

                    if post_copy_line not in constants_lines:
                        constants_lines.append(post_copy_line)
                    if post_run_line not in constants_lines:
                        constants_lines.append(post_run_line)

                sections[constants_section] = constants_lines

                present_section = '[Present]'
                present_lines = sections.get(present_section, [])

                active_block_start = -1
                active_block_end = -1

                for i, line in enumerate(present_lines):
                    if line.strip() == f"if {self.active_swapkey} == {self.active_value}":
                        active_block_start = i
                    elif active_block_start >= 0 and line.strip() == "endif":
                        active_block_end = i
                        break

                if active_block_start >= 0 and active_block_end >= 0:
                    for base_name, hash_prefix in processed_base_names:
                        run_line = f"    run = CustomShader_{base_name}_1Anim"
                        if run_line not in present_lines[active_block_start:active_block_end]:
                            present_lines.insert(active_block_end, run_line)
                else:
                    present_lines.append("")
                    present_lines.append(f"if {self.active_swapkey} == {self.active_value}")
                    for base_name, hash_prefix in processed_base_names:
                        present_lines.append(f"    run = CustomShader_{base_name}_1Anim")
                    present_lines.append("endif")

                sections[present_section] = present_lines

                self._write_ordered_dict_to_ini(sections, ini_file, preserved_tail_content)

            os.chdir(original_cwd)
            print("多文件配置生成完成！")

        except Exception as e:
            if 'original_cwd' in locals() and os.path.exists(original_cwd):
                os.chdir(original_cwd)
            print(f"多文件配置生成过程中出错: {str(e)}")
            import traceback
            traceback.print_exc()


classes = (
    SSMTNode_PostProcess_MultiFile,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
