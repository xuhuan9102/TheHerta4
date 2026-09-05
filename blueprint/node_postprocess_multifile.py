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
from ..common.mod_path_compat import collect_stale_texture_override_position_alias_names
from ..common.mod_path_compat import ensure_resource_alias_section
from ..common.mod_path_compat import find_base_position_resource_name
from ..common.mod_path_compat import iter_position_buffer_candidates
from ..common.mod_path_compat import is_stale_texture_override_position_copy_desc_line
from ..common.object_prefix_helper import ObjectPrefixHelper
try:
    from . import deform_chain
except ImportError:  # 测试 stub 包无 __path__ 时退化为绝对导入
    from blueprint import deform_chain


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

    def _resource_name_from_prefix(self, resource_prefix: str) -> str:
        return f"Resource_{resource_prefix}_Position"

    def _find_existing_base_resource_name(self, sections, hash_filter: str, base_name: str) -> str:
        normalized_base_name = str(base_name or "").strip()
        normalized_hash_filter = str(hash_filter or "").strip()

        preferred_canonical_name = self._resource_name_from_prefix(self._hash_to_resource_prefix(normalized_base_name))
        legacy_name = f"Resource{self._hash_to_resource_prefix(normalized_hash_filter)}Position"
        return find_base_position_resource_name(
            sections,
            normalized_hash_filter,
            base_name=normalized_base_name,
            preferred_names=[preferred_canonical_name, legacy_name],
            fallback_name=preferred_canonical_name,
        )

    def draw_buttons(self, context, layout):
        layout.prop(self, "hash_values")
        layout.prop(self, "animation_swapkey")
        layout.prop(self, "active_swapkey")
        layout.prop(self, "active_value")
        layout.prop(self, "comment", text="备注")

        if not NUMPY_AVAILABLE:
            layout.label(text="警告: 未安装numpy库，功能不可用", icon='ERROR')

    def _get_vertex_attrs_node(self):
        # 「顶点属性定义」节点已下线：此处仅兼容旧蓝图文件。残留节点可能已是
        # 未定义类型，必须探测方法存在再使用。
        def _as_vertex_attrs(candidate):
            if (
                candidate is not None
                and getattr(candidate, "bl_idname", "") == 'SSMTNode_PostProcess_VertexAttrs'
                and callable(getattr(candidate, "get_vertex_struct_definition", None))
            ):
                return candidate
            return None

        if not self.inputs[0].is_linked:
            return None

        source_node = self.inputs[0].links[0].from_node
        matched = _as_vertex_attrs(source_node)
        if matched is not None:
            return matched

        if source_node.inputs[0].is_linked:
            prev_node = source_node.inputs[0].links[0].from_node
            return _as_vertex_attrs(prev_node)

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
        return "struct VertexAttributes {\n    float3 position;\n    float3 normal;\n    float4 tangent;\n};"

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

    @staticmethod
    def _compute_dispatch_group_count(vertex_count, threads_per_group=16):
        vertex_count = int(vertex_count or 0)
        threads_per_group = max(1, int(threads_per_group or 1))
        return max(1, (vertex_count + threads_per_group - 1) // threads_per_group)

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

        ib_hashes = OrderedDict()
        for hash_value in hash_list:
            prefix_info = ObjectPrefixHelper.extract_prefix_info(hash_value)
            if prefix_info:
                prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
                if draw_ib:
                    ib_hashes[draw_ib] = True
                    continue

            normalized_hash_value = str(hash_value or "").strip()
            if normalized_hash_value.upper().startswith("LOD") and "." in normalized_hash_value:
                normalized_hash_value = normalized_hash_value.split(".", 1)[1]

            if '-' in normalized_hash_value:
                ib_hashes[normalized_hash_value.split('-', 1)[0]] = True
            elif normalized_hash_value:
                ib_hashes[normalized_hash_value] = True

        return list(ib_hashes.keys())

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
            # 这里只压缩 Position 差异，保证多文件切换时只写入真正变化的顶点数据。
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
        preserved_driver_content = ""

        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            preserved_driver_content, content = self.split_anim_driver_block_content(content)
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
            return None, "", ""
        return sections, preserved_tail_content, preserved_driver_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content=""):
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            if preserved_driver_content:
                f.write(preserved_driver_content)
                if not preserved_driver_content.endswith(chr(10)):
                    f.write(chr(10))
                f.write(chr(10))
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write("\n")

            if preserved_tail_content:
                f.write("\n")
                f.write(preserved_tail_content)

    def execute_postprocess(self, mod_export_path):
        print(f"MultiFile postprocess start, output: {mod_export_path}")

        if not NUMPY_AVAILABLE:
            print("MultiFile postprocess requires numpy.")
            return

        hash_values = self._parse_hash_values(self.hash_values)
        if not hash_values:
            print("No valid hash values were provided.")
            return

        original_cwd = os.getcwd()
        try:
            os.chdir(mod_export_path)

            ini_files = glob.glob("*.ini")
            if not ini_files:
                print("No ini file found in output path.")
                return

            meshes_folders = []
            for index in range(1, 1000):
                folder_name = f"Meshes{index:02d}"
                if os.path.exists(os.path.join(mod_export_path, folder_name)):
                    meshes_folders.append(folder_name)
                else:
                    break

            if len(meshes_folders) < 2:
                print("At least Meshes01 and Meshes02 are required for multifile postprocess.")
                return

            shader_source_path = self._get_shader_source_path()
            if shader_source_path and os.path.exists(shader_source_path):
                dest_res_dir = os.path.join(mod_export_path, "res")
                os.makedirs(dest_res_dir, exist_ok=True)
                shader_dest_path = os.path.join(dest_res_dir, "merge_anim_packed_delta.hlsl")
                shutil.copy2(shader_source_path, shader_dest_path)
                self._update_shader_file(shader_dest_path)

            for ini_file in ini_files:
                ini_file_path = os.path.join(mod_export_path, ini_file)
                self._create_cumulative_backup(ini_file_path, mod_export_path)
                sections, preserved_tail_content, preserved_driver_content = self._read_ini_to_ordered_dict(ini_file_path)
                if not sections:
                    continue

                for section_name in list(sections.keys()):
                    if not (section_name.startswith("[Resource") and section_name.endswith("]")):
                        continue
                    resource_name = section_name[1:-1]
                    remapped_lines = []
                    for line in sections[section_name]:
                        updated_line = line
                        for buf_folder in meshes_folders[1:]:
                            old_path = f"filename = {buf_folder}/"
                            if old_path in updated_line:
                                updated_line = updated_line.replace(old_path, "filename = Meshes01/")
                                break
                        remapped_lines.append(updated_line)
                    sections[section_name] = remapped_lines
                    if "Position" in resource_name:
                        # 锚点别名统一为共享的 _0（接力协议 v3；旧版 _1 由 ensure 的
                        # source_candidates 兼容承接，finalize 时迁移）
                        ensure_resource_alias_section(
                            sections,
                            resource_name,
                            "_0",
                            source_candidates=[resource_name, f"{resource_name}_1"],
                        )

                processed_entries = []
                meshes01_path = os.path.join(mod_export_path, "Meshes01")
                if not os.path.exists(meshes01_path):
                    print(f"Meshes01 folder missing: {meshes01_path}")
                    continue

                for hash_value in hash_values:
                    try:
                        base_candidates = iter_position_buffer_candidates(meshes01_path, hash_value)
                    except Exception as exc:
                        print(f"Failed to scan Meshes01 for {hash_value}: {exc}")
                        continue

                    if not base_candidates:
                        print(f"No Position buffer matched {hash_value} under Meshes01.")
                        continue

                    base_candidate = base_candidates[0]
                    base_position_file = base_candidate["filename"]
                    base_name = base_candidate["stem"]
                    hash_prefix = self._hash_to_resource_prefix(base_name)

                    base_meshes_full_path = os.path.join(mod_export_path, "Meshes01", base_position_file)
                    base_meshes = self._read_Meshes_file(base_meshes_full_path)
                    if base_meshes is None:
                        continue

                    processed_frames = []
                    for meshes_folder in meshes_folders[1:]:
                        meshes_folder_path = os.path.join(mod_export_path, meshes_folder)
                        try:
                            target_candidates = iter_position_buffer_candidates(meshes_folder_path, hash_value)
                        except Exception as exc:
                            print(f"Failed to scan {meshes_folder} for {hash_value}: {exc}")
                            continue

                        if not target_candidates:
                            continue

                        target_position_file = target_candidates[0]["filename"]
                        target_meshes_full_path = os.path.join(mod_export_path, meshes_folder, target_position_file)
                        if not os.path.exists(target_meshes_full_path):
                            continue

                        target_meshes = self._read_Meshes_file(target_meshes_full_path)
                        if target_meshes is None:
                            continue

                        map_array, pos_deltas_array = self._create_packed_Meshess(base_meshes, target_meshes, True)
                        pos_output_path = os.path.join(mod_export_path, meshes_folder, f"{base_name}-Position_packed_pos_delta.buf")
                        map_output_path = os.path.join(mod_export_path, meshes_folder, f"{base_name}-Position_map.buf")
                        self._write_Meshes_file(pos_deltas_array, pos_output_path)
                        self._write_Meshes_file(map_array, map_output_path)

                        folder_num = int(meshes_folder.replace("Meshes", ""))
                        sections[f"[Resource_{hash_prefix}_Position{folder_num:02d}_packed_pos_delta]"] = [
                            "type = Buffer",
                            "stride = 12",
                            f"filename = {meshes_folder}/{base_name}-Position_packed_pos_delta.buf",
                        ]
                        sections[f"[Resource_{hash_prefix}_Position{folder_num:02d}_Map]"] = [
                            "type = Buffer",
                            "stride = 4",
                            f"filename = {meshes_folder}/{base_name}-Position_map.buf",
                        ]
                        processed_frames.append((folder_num, meshes_folder))

                    if not processed_frames:
                        continue

                    base_resource_name = self._find_existing_base_resource_name(sections, hash_value, base_name)
                    base_resource_alias = ensure_resource_alias_section(
                        sections,
                        base_resource_name,
                        "_0",
                        source_candidates=[base_resource_name, f"{base_resource_name}_1"],
                    )[1:-1]
                    if f"[{base_resource_alias}]" not in sections:
                        print(
                            "[MultiFile][ERROR] 未在 INI 中找到基础 Position Resource，"
                            f"无法创建 copy_desc 别名: [{base_resource_name}]；"
                            f"哈希值: {hash_value}；基础文件: {base_position_file}。"
                        )
                        continue

                    shader_section = f"[CustomShader_{base_name}_1Anim]"
                    shader_lines = []
                    if self.comment:
                        shader_lines.append("; " + self.comment)
                        shader_lines.append("")

                    for state_index, (folder_num, _meshes_folder) in enumerate(processed_frames, 1):
                        shader_lines.append(f"if {self.animation_swapkey} == {state_index}")
                        shader_lines.append(f"      cs-t51 = copy Resource_{hash_prefix}_Position{folder_num:02d}_packed_pos_delta")
                        shader_lines.append("endif")
                    shader_lines.append("")
                    for state_index, (folder_num, _meshes_folder) in enumerate(processed_frames, 1):
                        shader_lines.append(f"if {self.animation_swapkey} == {state_index}")
                        shader_lines.append(f"      cs-t75 = copy Resource_{hash_prefix}_Position{folder_num:02d}_Map")
                        shader_lines.append("endif")

                    shader_lines.append("")
                    shader_lines.append("    cs = ./res/merge_anim_packed_delta.hlsl")
                    shader_lines.append(f"    cs-u5 = copy {base_resource_alias}")
                    # 输出双写：中间资源 _mf（接力下一级）与规范名 X（单用兼容）
                    shader_lines.append(f"    {base_resource_name}_mf = ref cs-u5")
                    shader_lines.append(f"    {base_resource_name} = ref cs-u5")

                    vertex_count = self._get_vertex_count(sections, hash_value)
                    if not vertex_count:
                        try:
                            file_size = os.path.getsize(base_meshes_full_path)
                            vertex_size_bytes = self._get_vertex_size() * 4
                            vertex_count = file_size // vertex_size_bytes
                        except Exception:
                            vertex_count = 100000
                    if vertex_count == 0:
                        vertex_count = 100000

                    dispatch_count = self._compute_dispatch_group_count(vertex_count, threads_per_group=16)
                    shader_lines.append(f"    Dispatch = {dispatch_count}, 1, 1")
                    shader_lines.append("    cs-u5 = null")
                    shader_lines.append("    cs-t51 = null")
                    shader_lines.append("    cs-t75 = null")
                    sections[shader_section] = shader_lines

                    processed_entries.append(
                        {
                            "base_name": base_name,
                            "hash_prefix": hash_prefix,
                            "hash_value": hash_value,
                            "base_resource_name": base_resource_name,
                            "base_resource_alias": base_resource_alias,
                        }
                    )

                constants_section = "[Constants]"
                constants_lines = sections.get(constants_section, [])
                if not any(self.animation_swapkey in line for line in constants_lines):
                    constants_lines.append(f"global persist {self.animation_swapkey} = 0")
                if not any(self.active_swapkey in line for line in constants_lines):
                    constants_lines.append(f"global persist {self.active_swapkey} = 0")

                # 声明每个资源的运行时就位标志（接力协议 v3 §2.2-3）
                for entry in processed_entries:
                    ran_var = deform_chain.mf_ran_var(entry["base_resource_name"])
                    if not any(ran_var in line for line in constants_lines):
                        constants_lines.append(f"global persist {ran_var} = 0")

                for entry in processed_entries:
                    legacy_base_resource_name = f"Resource{entry['hash_prefix']}Position"
                    # 旧版遗留的 _1 锚点/post 复位行一并清理
                    legacy_post_copy_line = f"post {legacy_base_resource_name} = copy_desc {legacy_base_resource_name}_1"
                    legacy_post_copy_line_v1 = f"post {entry['base_resource_name']} = copy_desc {entry['base_resource_name']}_1"
                    post_copy_line = f"post {entry['base_resource_name']} = copy_desc {entry['base_resource_alias']}"
                    post_run_line = f"post run = CustomShader_{entry['base_name']}_1Anim"
                    stale_alias_names = []
                    stale_hash_filters = [
                        entry["hash_value"],
                        entry["base_name"],
                        entry["hash_prefix"],
                    ]
                    for stale_hash_filter in stale_hash_filters:
                        for stale_alias_name in collect_stale_texture_override_position_alias_names(
                            constants_lines,
                            stale_hash_filter,
                        ):
                            if stale_alias_name not in stale_alias_names:
                                stale_alias_names.append(stale_alias_name)
                    for stale_alias_name in stale_alias_names:
                        stale_section_name = f"[{stale_alias_name}]"
                        if stale_section_name in sections:
                            del sections[stale_section_name]
                        print(
                            "[MultiFile][WARNING] 检测到旧版本错误生成的 TextureOverride Position copy_desc，"
                            f"已移除别名 section: {stale_alias_name}；"
                            f"当前基础 Position 资源: {entry['base_resource_name']}。"
                        )
                    constants_lines = [
                        line
                        for line in constants_lines
                        if line != legacy_post_copy_line
                        and line != legacy_post_copy_line_v1
                        and line != post_copy_line
                        and line != post_run_line
                        and not any(
                            is_stale_texture_override_position_copy_desc_line(line, stale_hash_filter)
                            for stale_hash_filter in stale_hash_filters
                        )
                    ]
                    constants_lines.append(post_copy_line)
                    constants_lines.append(post_run_line)
                sections[constants_section] = constants_lines

                # 把 run 行迁入带 mf_ran 标志的激活块（接力协议 v3 §2.2-5）
                present_section = "[Present]"
                present_lines = sections.get(present_section, [])
                mf_ran_vars = [deform_chain.mf_ran_var(e["base_resource_name"]) for e in processed_entries]
                run_lines = [f"run = CustomShader_{e['base_name']}_1Anim" for e in processed_entries]
                present_lines = deform_chain.ensure_multifile_present_block(
                    present_lines,
                    self.active_swapkey,
                    self.active_value,
                    mf_ran_vars,
                    run_lines,
                )
                sections[present_section] = present_lines

                # 终态规整（幂等）：形态键条件锚定、接力块排序、复位行去重、_mf 声明
                deform_chain.finalize_deform_chain(sections)

                self._write_ordered_dict_to_ini(sections, ini_file_path, preserved_tail_content, preserved_driver_content)

            print("MultiFile postprocess completed.")
        except Exception as e:
            print(f"MultiFile postprocess failed: {str(e)}")
            import traceback
            traceback.print_exc()
        finally:
            if os.path.exists(original_cwd):
                os.chdir(original_cwd)


classes = (
    SSMTNode_PostProcess_MultiFile,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
