import bpy
import os
import glob
import re
import shutil
import struct
from collections import OrderedDict, defaultdict

from .node_postprocess_base import SSMTNode_PostProcess_Base
from .variable_registry import (
    allocate_uv_offset_variable_name,
    mark_variable_name_used,
    normalize_variable_name,
)
from ..common.mod_path_compat import ensure_resource_alias_section
from ..common.object_prefix_helper import ObjectPrefixHelper


UV_TYPE_SIZES = {
    'float': 4, 'float2': 8, 'float3': 12, 'float4': 16,
    'half': 2, 'half2': 4, 'half3': 6, 'half4': 8,
    'rgba8': 4,
}

UV_OFFSET_SUPPORTED_TYPES = {'float2', 'half2'}

COMMON_ZZMI_UV_LAYOUT = (
    ('rgba8', 'color', False),
    ('half2', 'uv0', True),
    ('float2', 'uv1', True),
    ('half2', 'uv2', True),
)

# 工作空间 DXGI Format -> 插件 UV 属性类型（仅用于映射；未知格式保留原样但计入字节数）
WORKSPACE_UV_FORMAT_TO_TYPE = {
    'R32_FLOAT': 'float',
    'R32G32_FLOAT': 'float2',
    'R32G32B32_FLOAT': 'float3',
    'R32G32B32A32_FLOAT': 'float4',
    'R16_FLOAT': 'half',
    'R16G16_FLOAT': 'half2',
    'R16G16B16A16_FLOAT': 'half4',
    'R8G8B8A8_UNORM': 'rgba8',
    'R8G8B8A8_SNORM': 'rgba8',
    'R8G8B8A8_UINT': 'rgba8',
}


def _resolve_workspace_game_type_by_prefix(prefix):
    """延迟导入工作空间格式解析（避免测试桩包触发模块链导入）。"""
    from ..common.submesh_metadata import resolve_workspace_game_type_by_prefix as _impl
    return _impl(prefix)


def workspace_uv_attributes_from_game_type(game_type):
    """从工作空间游戏类型构造 UV 属性布局（Texcoord 类别元素，顺序即流内顺序）。

    - COLOR 等非 TEXCOORD 语义元素计字节但不参与偏移（apply_offset=False）；
    - 未知 Format 的元素保留原始格式名并计入字节数（着色器侧跳过偏移，
      但总字节必须与实际 Texcoord stride 一致，避免误跳过）。
    """
    attributes = []
    stream_offset = 0
    for element in getattr(game_type, "D3D11ElementList", []) or []:
        if str(getattr(element, "Category", "") or "").upper() != "TEXCOORD":
            continue
        semantic = str(getattr(element, "SemanticName", "") or "").strip()
        semantic_index = int(getattr(element, "SemanticIndex", 0) or 0)
        byte_width = int(getattr(element, "ByteWidth", 0) or 0)
        if byte_width <= 0:
            continue
        element_format = str(getattr(element, "Format", "") or "").upper()
        attr_type = WORKSPACE_UV_FORMAT_TO_TYPE.get(element_format, element_format.lower() if element_format else "unknown")
        is_color = semantic.upper().startswith("COLOR")
        attributes.append({
            'name': semantic.lower() + str(semantic_index),
            'type': attr_type,
            'size': byte_width,
            'offset': stream_offset,
            'apply_offset': not is_color,
        })
        stream_offset += byte_width
    if stream_offset <= 0:
        return None
    return attributes


def uv_attributes_total_bytes(attributes):
    return sum(attr['size'] for attr in attributes or [])


class UVOffsetVariableItem(bpy.types.PropertyGroup):
    axis_name: bpy.props.StringProperty(name="轴", default="")  # type: ignore
    assigned_variable_name: bpy.props.StringProperty(name="Assigned Variable Name", default="")  # type: ignore

    def update_custom_variable_name(self, context):
        normalized = normalize_variable_name(self.custom_variable_name)
        if normalized != self.custom_variable_name:
            self.custom_variable_name = normalized
            return
        if normalized:
            mark_variable_name_used(normalized, context=context)

    custom_variable_name: bpy.props.StringProperty(
        name="Custom Variable Name",
        default="",
        update=update_custom_variable_name,
    )  # type: ignore


class SSMT_UL_UV_OFFSET_VARIABLE_MAPPINGS(bpy.types.UIList):
    bl_idname = "SSMT_UL_UV_OFFSET_VARIABLE_MAPPINGS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        del context, data, icon, active_data, active_propname, index
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=f"{item.axis_name} 偏移", icon='DRIVER')
            value_col = row.column(align=True)
            value_col.prop(item, "custom_variable_name", text="导出变量")
            assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
            value_col.label(text=f"预分配: ${assigned_name}" if assigned_name else "预分配: 未分配", icon='INFO')


class UVOffsetObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore
    start_vertex: bpy.props.IntProperty(name="起始顶点", default=-1)  # type: ignore
    end_vertex: bpy.props.IntProperty(name="结束顶点", default=-1)  # type: ignore


class SSMTNode_PostProcess_UVOffset(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_UVOffset'
    bl_label = 'UV偏移'
    bl_description = '按物体索引范围偏移所有UV通道，偏移量由预分配的XY变量驱动'

    uv_offset_variable_items: bpy.props.CollectionProperty(type=UVOffsetVariableItem)  # type: ignore
    uv_offset_variable_index: bpy.props.IntProperty(default=0)  # type: ignore
    uv_objects: bpy.props.CollectionProperty(type=UVOffsetObjectItem)  # type: ignore
    active_uv_object: bpy.props.IntProperty(default=0)  # type: ignore

    # ---------- 变量预分配（参考形态键配置节点） ----------

    def _backfill_uv_offset_variable_input(self, item):
        assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
        custom_name = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
        if assigned_name and not custom_name:
            item.custom_variable_name = assigned_name
            return True
        return False

    def ensure_uv_offset_variable_map(self, axes):
        existing_by_axis = {
            item.axis_name: item
            for item in self.uv_offset_variable_items
            if str(getattr(item, "axis_name", "") or "").strip()
        }
        created_count = 0
        backfilled_count = 0
        for axis in axes:
            axis = str(axis or "").strip()
            if not axis:
                continue
            existing = existing_by_axis.get(axis)
            if existing is None:
                item = self.uv_offset_variable_items.add()
                item.axis_name = axis
                item.assigned_variable_name = allocate_uv_offset_variable_name(axis)
                item.custom_variable_name = normalize_variable_name(item.assigned_variable_name)
                created_count += 1
            else:
                owned_names = (
                    getattr(existing, "assigned_variable_name", ""),
                    getattr(existing, "custom_variable_name", ""),
                )
                if not existing.assigned_variable_name:
                    existing.assigned_variable_name = allocate_uv_offset_variable_name(
                        axis,
                        preferred=existing.custom_variable_name,
                        owned_names=owned_names,
                    )
                elif self._backfill_uv_offset_variable_input(existing):
                    backfilled_count += 1
        return created_count, backfilled_count

    def get_uv_offset_variable_name(self, axis):
        axis = str(axis or "").strip() or "X"
        item = None
        for candidate in self.uv_offset_variable_items:
            if str(getattr(candidate, "axis_name", "") or "").strip() == axis:
                item = candidate
                break
        if item is None:
            item = self.uv_offset_variable_items.add()
            item.axis_name = axis
            item.assigned_variable_name = allocate_uv_offset_variable_name(axis)
            item.custom_variable_name = normalize_variable_name(item.assigned_variable_name)
        owned_names = (
            getattr(item, "assigned_variable_name", ""),
            getattr(item, "custom_variable_name", ""),
        )
        custom_name = normalize_variable_name(item.custom_variable_name)
        if custom_name:
            return f"${custom_name}"
        assigned_name = normalize_variable_name(item.assigned_variable_name)
        if not assigned_name:
            assigned_name = allocate_uv_offset_variable_name(axis, owned_names=owned_names)
            item.assigned_variable_name = assigned_name
        return f"${assigned_name}"

    def get_uv_offset_export_variable_names(self):
        return (self.get_uv_offset_variable_name("X"), self.get_uv_offset_variable_name("Y"))

    # ---------- UV 布局解析（工作空间驱动） ----------
    # 「UV 属性定义」节点与动态输入口已下线：每个 IB 的 Texcoord 布局直接从
    # 工作空间 SubmeshJson 读取（逐 IB 动态适配），不可解析时回退默认布局。

    def _collect_uv_prefixes(self):
        """从物体列表解析出独立的 8 位 IB 前缀（保序去重）。"""
        prefixes = []
        seen = set()
        for item in self.uv_objects:
            obj_name = str(getattr(item, "object_name", "") or "").strip()
            if not obj_name:
                continue
            obj_hash = self._extract_hash_from_name(obj_name)
            if not obj_hash:
                continue
            h_prefix = self._extract_hash_prefix(obj_hash)
            if not h_prefix:
                continue
            if h_prefix in seen:
                continue
            seen.add(h_prefix)
            prefixes.append(h_prefix)
        return prefixes

    def _get_workspace_uv_attributes(self, prefix):
        """按 IB 前缀从工作空间解析 Texcoord 布局（每个 IB 独立，动态处理）。

        工作空间 SubmeshJson 记录每个 IB 的真实 Texcoord 类别元素布局，
        优先以它为准，替代用户手填的 UV 属性定义节点 —— 这样 20/24/48 字节
        等异构布局都能自动生成，不再因「默认布局与 stride 不一致」被跳过。
        """
        prefix = str(prefix or "").strip()
        if not prefix:
            return None
        cache = getattr(self, "_workspace_uv_attributes_cache", None)
        if cache is None:
            cache = {}
            try:
                self._workspace_uv_attributes_cache = cache
            except Exception:
                pass
        if prefix in cache:
            return cache[prefix]
        attributes = None
        try:
            game_type = _resolve_workspace_game_type_by_prefix(prefix)
            if game_type is not None:
                attributes = workspace_uv_attributes_from_game_type(game_type)
        except Exception:
            attributes = None
        cache[prefix] = attributes
        return attributes

    def _get_uv_attributes_for_prefix(self, prefix):
        """取指定 IB 前缀对应的 UV 属性布局。

        优先级：工作空间真实布局（逐 IB 动态适配）→ 默认布局（旧兜底）。
        不再需要连接 UV 属性定义节点。
        """
        workspace_attributes = self._get_workspace_uv_attributes(prefix)
        if workspace_attributes:
            return workspace_attributes

        return self._default_uv_attributes()

    @staticmethod
    def _default_uv_attributes():
        default_attributes = []
        default_offset = 0
        for attr_type, attr_name, apply_offset in COMMON_ZZMI_UV_LAYOUT:
            size = UV_TYPE_SIZES.get(attr_type, 0)
            if size <= 0:
                continue
            default_attributes.append({
                'name': attr_name,
                'type': attr_type,
                'size': size,
                'offset': default_offset,
                'apply_offset': apply_offset,
            })
            default_offset += size
        return default_attributes

    def _get_shader_source_path(self):
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            asset_source_dir = os.path.join(addon_dir, "Toolset")
            return os.path.join(asset_source_dir, "uv_offset_anim.hlsl")
        except Exception as e:
            print(f"获取UV偏移着色器模板路径时出错: {e}")
            return None

    @staticmethod
    def _compute_dispatch_group_count(vertex_count, threads_per_group=16):
        vertex_count = int(vertex_count or 0)
        threads_per_group = max(1, int(threads_per_group or 1))
        return max(1, (vertex_count + threads_per_group - 1) // threads_per_group)

    # ---------- INI 解析与顶点范围计算（参考形态键配置节点） ----------

    @staticmethod
    def _normalize_ini_section_lookup_key(section_name):
        normalized_name = str(section_name or "").strip()
        if normalized_name.startswith('[') and normalized_name.endswith(']'):
            normalized_name = normalized_name[1:-1].strip()
        return normalized_name.casefold()

    def _build_ini_section_lookup(self, sections):
        section_lookup = {}
        for section_name in sections.keys():
            normalized_name = self._normalize_ini_section_lookup_key(section_name)
            if normalized_name and normalized_name not in section_lookup:
                section_lookup[normalized_name] = section_name
        return section_lookup

    @staticmethod
    def _extract_ini_assignment_value(line, key_name):
        stripped_line = str(line or "").strip()
        if '=' not in stripped_line:
            return ""
        assignment_name, value = stripped_line.split('=', 1)
        if assignment_name.strip().casefold() != str(key_name or "").strip().casefold():
            return ""
        return value.strip()

    @staticmethod
    def _extract_run_target_name(line):
        target_name = SSMTNode_PostProcess_UVOffset._extract_ini_assignment_value(line, "run")
        if target_name.startswith('[') and target_name.endswith(']'):
            target_name = target_name[1:-1].strip()
        return target_name

    def _resolve_run_section_name(self, section_lookup, run_target_name):
        if not run_target_name:
            return ""
        return section_lookup.get(self._normalize_ini_section_lookup_key(run_target_name), "")

    @staticmethod
    def _parse_draw_command_line(stripped_line):
        stripped_line = str(stripped_line or "").strip()
        if '=' not in stripped_line:
            return None
        command_name, raw_params = stripped_line.split('=', 1)
        command_name = command_name.strip().casefold()

        def safe_int_parse(value):
            try:
                value = str(value or "").strip()
                if value.lstrip('-').isdigit():
                    return int(value)
            except Exception:
                pass
            return None

        parts = [part.strip() for part in raw_params.strip().split(',')]
        if command_name == 'drawindexed' and len(parts) == 3:
            draw_params = (
                safe_int_parse(parts[0]),
                safe_int_parse(parts[1]),
                safe_int_parse(parts[2]),
            )
            if all(value is not None for value in draw_params):
                return ('drawindexed', draw_params)
        elif command_name == 'draw' and len(parts) == 2:
            draw_params = (
                safe_int_parse(parts[0]),
                safe_int_parse(parts[1]),
            )
            if all(value is not None for value in draw_params):
                return ('draw', draw_params)
        return None

    @staticmethod
    def _resolve_ib_resource_path(resource_map, ib_resource_ref):
        resource_ref = str(ib_resource_ref or "").strip()
        if not resource_ref:
            return None

        if resource_ref.lower().startswith('ref '):
            resource_ref = resource_ref[4:].strip()
        if resource_ref.startswith('[') and resource_ref.endswith(']'):
            resource_ref = resource_ref[1:-1].strip()
        return resource_map.get(resource_ref.casefold())

    def _resolve_draw_command_from_section(self, sections, section_lookup, section_name, run_path=None, visited_sections=None):
        if not section_name:
            return None

        if visited_sections is None:
            visited_sections = set()
        if section_name in visited_sections:
            return None

        visited_sections = set(visited_sections)
        visited_sections.add(section_name)
        lines = sections.get(section_name, []) or []
        current_run_path = list(run_path or [])

        for line_index, line in enumerate(lines):
            parsed = self._parse_draw_command_line(line.strip())
            if parsed is not None:
                draw_type, draw_params = parsed
                return {
                    'draw_type': draw_type,
                    'draw_params': draw_params,
                    'draw_section_name': section_name,
                    'draw_line_index': line_index,
                    'run_path': current_run_path,
                }

            run_target_name = self._extract_run_target_name(line)
            if not run_target_name:
                continue
            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue
            result = self._resolve_draw_command_from_section(
                sections,
                section_lookup,
                target_section_name,
                run_path=current_run_path + [(section_name, line_index)],
                visited_sections=visited_sections,
            )
            if result is not None:
                return result

        return None

    def _resolve_draw_command_from_mesh_block(self, sections, section_lookup, section_name, lines, mesh_line_index, block_end_index):
        for line_index in range(mesh_line_index + 1, block_end_index):
            parsed = self._parse_draw_command_line(lines[line_index].strip())
            if parsed is not None:
                draw_type, draw_params = parsed
                return {
                    'draw_type': draw_type,
                    'draw_params': draw_params,
                    'draw_section_name': section_name,
                    'draw_line_index': line_index,
                    'run_path': [],
                }

            run_target_name = self._extract_run_target_name(lines[line_index])
            if not run_target_name:
                continue
            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue
            result = self._resolve_draw_command_from_section(
                sections,
                section_lookup,
                target_section_name,
                run_path=[(section_name, line_index)],
                visited_sections={section_name},
            )
            if result is not None:
                return result

        return None

    def _resolve_ib_path_from_section(self, sections, section_lookup, resource_map, section_name, visited_sections=None):
        if not section_name:
            return None

        if visited_sections is None:
            visited_sections = set()
        if section_name in visited_sections:
            return None

        visited_sections = set(visited_sections)
        visited_sections.add(section_name)
        lines = sections.get(section_name, []) or []

        for line in lines:
            ib_resource_ref = self._extract_ini_assignment_value(line, "ib")
            ib_path = self._resolve_ib_resource_path(resource_map, ib_resource_ref)
            if ib_path:
                return ib_path

        for line in lines:
            run_target_name = self._extract_run_target_name(line)
            if not run_target_name:
                continue
            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue
            ib_path = self._resolve_ib_path_from_section(
                sections,
                section_lookup,
                resource_map,
                target_section_name,
                visited_sections=visited_sections,
            )
            if ib_path:
                return ib_path

        return None

    def _resolve_ib_path_from_anchor(self, sections, section_lookup, resource_map, section_name, anchor_line_index):
        lines = sections.get(section_name, []) or []
        anchor_line_index = min(max(int(anchor_line_index or 0), 0), len(lines))

        for line_index in range(anchor_line_index - 1, -1, -1):
            stripped_line = lines[line_index].strip()
            if stripped_line.startswith('if ') or stripped_line == 'endif':
                continue

            ib_resource_ref = self._extract_ini_assignment_value(stripped_line, "ib")
            ib_path = self._resolve_ib_resource_path(resource_map, ib_resource_ref)
            if ib_path:
                return ib_path

            run_target_name = self._extract_run_target_name(stripped_line)
            if not run_target_name:
                continue
            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue
            ib_path = self._resolve_ib_path_from_section(
                sections,
                section_lookup,
                resource_map,
                target_section_name,
                visited_sections={section_name},
            )
            if ib_path:
                return ib_path

        return None

    def _parse_ini_for_draw_info(self, sections, base_path):
        draw_info = {}
        resource_map = {}
        section_lookup = self._build_ini_section_lookup(sections)

        for section_name, lines in sections.items():
            if not section_name.lower().startswith('[resource'):
                continue
            filename = next(
                (
                    value
                    for value in (
                        self._extract_ini_assignment_value(line, "filename")
                        for line in lines
                    )
                    if value
                ),
                None,
            )
            if filename:
                resource_name = self._normalize_ini_section_lookup_key(section_name)
                resource_map[resource_name] = os.path.join(base_path, filename.replace('/', os.sep))

        for section_name, lines in sections.items():
            if not section_name.lower().startswith('[textureoverride'):
                continue

            for mesh_line_index, line in enumerate(lines):
                stripped_line = line.strip()
                mesh_match = re.search(r'\[mesh:([^\]]+)\]', stripped_line)
                if not mesh_match:
                    continue

                current_mesh_name = mesh_match.group(1).strip()
                block_end_index = len(lines)
                for next_index in range(mesh_line_index + 1, len(lines)):
                    if re.search(r'\[mesh:([^\]]+)\]', lines[next_index].strip()):
                        block_end_index = next_index
                        break

                resolved_draw = self._resolve_draw_command_from_mesh_block(
                    sections,
                    section_lookup,
                    section_name,
                    lines,
                    mesh_line_index,
                    block_end_index,
                )
                if resolved_draw is None:
                    continue

                draw_params = resolved_draw.get('draw_params')
                draw_type = resolved_draw.get('draw_type')
                draw_section_name = resolved_draw.get('draw_section_name', '')
                draw_line_index = resolved_draw.get('draw_line_index', -1)
                run_path = list(resolved_draw.get('run_path', []) or [])

                ib_path = None
                if draw_type == 'draw':
                    # 非索引绘制不需要 IB，直接按顶点范围处理
                    ib_path = None
                elif run_path:
                    ib_path = self._resolve_ib_path_from_anchor(
                        sections, section_lookup, resource_map, draw_section_name, draw_line_index,
                    )
                    if not ib_path:
                        for anchor_section_name, anchor_line_index in reversed(run_path):
                            ib_path = self._resolve_ib_path_from_anchor(
                                sections, section_lookup, resource_map, anchor_section_name, anchor_line_index,
                            )
                            if ib_path:
                                break
                else:
                    ib_path = self._resolve_ib_path_from_anchor(
                        sections, section_lookup, resource_map, draw_section_name, draw_line_index,
                    )

                if draw_type == 'drawindexed' and not ib_path:
                    continue

                draw_info.setdefault(current_mesh_name, []).append({
                    'draw_type': draw_type,
                    'draw_params': draw_params,
                    'ib_path': ib_path,
                })

        return draw_info

    def _calculate_vertex_range(self, ib_path, draw_type, draw_params):
        if draw_type == 'draw':
            vertex_count, start_vertex_location = draw_params
            return (start_vertex_location, start_vertex_location + vertex_count - 1)

        index_count, start_index_location, base_vertex_location = draw_params
        if not os.path.isfile(ib_path):
            print(f"IB 文件不存在: {ib_path}")
            return None, None
        try:
            file_size = os.path.getsize(ib_path)
            seek_pos = start_index_location * 4
            read_size = index_count * 4
            if seek_pos >= file_size:
                return None, None
            with open(ib_path, 'rb') as f:
                f.seek(seek_pos)
                data = f.read(read_size)
            if len(data) < read_size:
                return None, None
            # 与 node_postprocess_shapekey._calculate_vertex_range 同理：
            # 烘焙/查找表按静态 VB 行索引，base_vertex 是运行时 SO 前缀偏移，
            # 不能加进范围（否则范围整体平移 base 行，跨物体边界互相污染）。
            indices = list(struct.unpack(f'<{index_count}I', data))
            return (min(indices), max(indices)) if indices else (None, None)
        except Exception as e:
            print(f"计算顶点范围时出错: {e}")
            return None, None

    def _compute_range_for_entries(self, entries):
        ranges = []
        for entry in entries or []:
            start_v, end_v = self._calculate_vertex_range(
                entry.get('ib_path'),
                entry.get('draw_type'),
                entry.get('draw_params'),
            )
            if start_v is not None and end_v is not None:
                ranges.append((start_v, end_v))
        if not ranges:
            return None
        return (min(r[0] for r in ranges), max(r[1] for r in ranges))

    def _extract_hash_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
            bare_unique_str = str(prefix_parts.get("bare_unique_str", "") or "").strip()
            if bare_unique_str:
                return bare_unique_str

        match = re.match(r'^([a-f0-9]{8}-[a-f0-9]+(?:-[a-f0-9]+)?)', obj_name)
        if match:
            return match.group(1)
        match = re.match(r'^([a-f0-9]{8})', obj_name)
        if match:
            return match.group(1)
        return None

    def _extract_hash_prefix(self, hash_val):
        if hash_val:
            prefix_parts = ObjectPrefixHelper.parse_prefix_parts(hash_val)
            draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
            if draw_ib:
                return draw_ib
            normalized_hash_val = str(hash_val or "").strip()
            if normalized_hash_val.upper().startswith("LOD") and "." in normalized_hash_val:
                normalized_hash_val = normalized_hash_val.split(".", 1)[1]
            return normalized_hash_val.split('-')[0]
        return None

    def _extract_alias_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            _prefix, _separator, base_name = ObjectPrefixHelper.split_name_and_prefix(
                obj_name,
                prefix_info[0],
                prefix_info[1],
            )
            return base_name if base_name else obj_name

        obj_hash = self._extract_hash_from_name(obj_name)
        if obj_hash:
            remainder = obj_name[len(obj_hash):]
            alias = remainder.lstrip('.').lstrip('_')
            return alias if alias else obj_name
        return obj_name

    @staticmethod
    def _strip_runtime_copy_suffix(name):
        stripped = re.sub(r'(_(?:chain|dup|copy|BPE)\d*)+$', '', name, flags=re.IGNORECASE)
        return stripped

    @staticmethod
    def _strip_object_suffix(name):
        stripped = re.sub(r'(_(?:chain|dup|copy|BPE)\d*)+$', '', name, flags=re.IGNORECASE)
        return stripped

    def _resolve_object_range(self, obj_name, draw_info):
        if obj_name in draw_info:
            return self._compute_range_for_entries(draw_info[obj_name])

        obj_hash = self._extract_hash_from_name(obj_name)
        if not obj_hash:
            return None
        obj_prefix = self._extract_hash_prefix(obj_hash)
        obj_alias = self._extract_alias_from_name(obj_name)
        obj_runtime_alias = self._strip_runtime_copy_suffix(obj_alias).casefold()
        obj_base_alias = self._strip_object_suffix(obj_alias).casefold()

        alias_results = []
        for mesh_name, entries in draw_info.items():
            mesh_hash = self._extract_hash_from_name(mesh_name)
            if not mesh_hash:
                continue
            if self._extract_hash_prefix(mesh_hash) != obj_prefix:
                continue
            mesh_alias = self._extract_alias_from_name(mesh_name)
            mesh_runtime_alias = self._strip_runtime_copy_suffix(mesh_alias).casefold()
            mesh_base_alias = self._strip_object_suffix(mesh_alias).casefold()
            if obj_runtime_alias == mesh_runtime_alias or obj_base_alias == mesh_base_alias:
                resolved = self._compute_range_for_entries(entries)
                if resolved is not None:
                    alias_results.append(resolved)

        if len(alias_results) == 1:
            return alias_results[0]
        if len(alias_results) > 1:
            print(f"[UVOffset] 物体 '{obj_name}' 的别名匹配到多个候选，跳过自动映射")
            return None

        # 唯一同前缀候选兜底（与形态键配置节点行为一致）
        prefix_results = []
        for mesh_name, entries in draw_info.items():
            mesh_hash = self._extract_hash_from_name(mesh_name)
            if not mesh_hash or self._extract_hash_prefix(mesh_hash) != obj_prefix:
                continue
            resolved = self._compute_range_for_entries(entries)
            if resolved is not None:
                prefix_results.append(resolved)
        if len(prefix_results) == 1:
            return prefix_results[0]
        if len(prefix_results) > 1:
            print(f"[UVOffset] 物体 '{obj_name}' 的同前缀候选存在多个，跳过自动映射")
            return None
        return None

    # ---------- 导出生成 ----------

    def _find_texcoord_resource(self, sections, h_prefix):
        preferred_names = [
            f"Resource{h_prefix}Texcoord",
            f"Resource_{h_prefix}_Texcoord",
        ]
        matches = []
        for section_name in sections.keys():
            name = str(section_name or "").strip()
            if not (name.startswith('[') and name.endswith(']')):
                continue
            resource_name = name[1:-1].strip()
            if not resource_name.lower().startswith('resource'):
                continue
            if not resource_name.lower().endswith('texcoord'):
                continue
            if h_prefix.casefold() in resource_name.casefold():
                matches.append(resource_name)

        for preferred_name in preferred_names:
            if preferred_name in matches:
                return preferred_name
        return matches[0] if matches else None

    def _get_vertex_count(self, sections, h_prefix):
        for section_name, lines in sections.items():
            match = re.match(r'\[TextureOverride_([a-f0-9]{8}(?:[_-][a-f0-9]+)*)_[^_]*_VertexLimitRaise\]', section_name)
            if not match:
                continue
            hash_val = match.group(1).replace('_', '-')
            if self._extract_hash_prefix(hash_val) != h_prefix:
                continue
            for line in lines:
                stripped = line.strip()
                if stripped.startswith('override_vertex_count'):
                    try:
                        return int(stripped.split('=', 1)[1].strip())
                    except (ValueError, IndexError):
                        continue
        return None

    def _resolve_texcoord_file_info(self, sections, mod_export_path, texcoord_resource):
        section_lines = sections.get(f"[{texcoord_resource}]", []) or []
        filename = None
        stride = 20
        for line in section_lines:
            value = self._extract_ini_assignment_value(line, "filename")
            if value:
                filename = value
            value = self._extract_ini_assignment_value(line, "stride")
            if value:
                try:
                    stride = int(value)
                except (ValueError, TypeError):
                    pass
        if filename:
            file_path = os.path.join(mod_export_path, filename.replace('/', os.sep))
            if os.path.exists(file_path):
                return file_path, stride
        return None, stride

    @staticmethod
    def _generate_uv_apply_code(attributes, stride):
        if stride <= 0 or stride > 64:
            return None, f"UV流字节数 {stride} 超出支持范围(1-64)"
        if stride % 4 != 0:
            return None, f"UV流字节数 {stride} 不是4的倍数，无法按uint数组处理"

        uints_per_vertex = stride // 4
        apply_lines = []
        skipped = []
        index = 0
        for attr in attributes or []:
            attr_type = attr.get('type', '')
            attr_name = attr.get('name', '')
            attr_offset = int(attr.get('offset', 0) or 0)
            apply_offset = bool(attr.get('apply_offset', True))
            if not apply_offset:
                continue
            if attr_type not in UV_OFFSET_SUPPORTED_TYPES:
                skipped.append(f"{attr_name}({attr_type})")
                continue
            if attr_offset % 4 != 0:
                skipped.append(f"{attr_name}(未4字节对齐)")
                continue

            var_name = f"v_uv_{index}"
            index += 1
            uint_index = attr_offset // 4
            if attr_type == 'float2' and uint_index + 2 > uints_per_vertex:
                skipped.append(f"{attr_name}(越界)")
                continue
            if attr_type == 'half2' and uint_index + 1 > uints_per_vertex:
                skipped.append(f"{attr_name}(越界)")
                continue

            if attr_type == 'float2':
                apply_lines.extend([
                    f"    // float2 {attr_name} @ {attr_offset}",
                    f"    float2 {var_name} = asfloat(uint2(data[{uint_index}], data[{uint_index + 1}]));",
                    f"    {var_name} += uv_offset;",
                    f"    data[{uint_index}] = asuint({var_name}.x);",
                    f"    data[{uint_index + 1}] = asuint({var_name}.y);",
                ])
            elif attr_type == 'half2':
                apply_lines.extend([
                    f"    // half2 {attr_name} @ {attr_offset}",
                    f"    uint p_{var_name} = data[{uint_index}];",
                    f"    float2 {var_name} = float2(f16tof32(p_{var_name} & 0xFFFFu), f16tof32(p_{var_name} >> 16));",
                    f"    {var_name} = clamp({var_name} + uv_offset, -65504.0, 65504.0);",
                    f"    data[{uint_index}] = (f32tof16({var_name}.x) & 0xFFFFu) | ((f32tof16({var_name}.y) & 0xFFFFu) << 16);",
                ])

        if not apply_lines:
            apply_lines = ["    // 未配置可偏移的 float2/half2 UV 属性，本帧不产生偏移"]
        warning = ("未参与偏移的属性: " + ", ".join(skipped)) if skipped else None
        return "\n".join(apply_lines), warning

    def _update_shader_file(self, shader_path, ranges, attributes, stride):
        try:
            with open(shader_path, 'r', encoding='utf-8') as f:
                content = f.read()

            uints_per_vertex = stride // 4
            content = re.sub(
                r"static const uint UV_STREAM_BYTES_PER_VERTEX = \d+;",
                f"static const uint UV_STREAM_BYTES_PER_VERTEX = {int(stride)};",
                content,
            )
            content = re.sub(
                r"static const uint UV_STREAM_UINTS_PER_VERTEX = \d+;",
                f"static const uint UV_STREAM_UINTS_PER_VERTEX = {uints_per_vertex};",
                content,
            )

            if ranges:
                range_lines = [
                    f"static const uint UV_OFFSET_RANGE_COUNT = {len(ranges)};",
                    "",
                    "bool uv_offset_in_range(uint vertex_id)",
                    "{",
                ]
                for start_v, end_v in ranges:
                    range_lines.append(f"    if (vertex_id >= {int(start_v)}u && vertex_id <= {int(end_v)}u) return true;")
                range_lines.append("    return false;")
                range_lines.append("}")
                range_block = "\n".join(range_lines)
            else:
                range_block = (
                    "static const uint UV_OFFSET_RANGE_COUNT = 0;\n"
                    "\n"
                    "bool uv_offset_in_range(uint vertex_id)\n"
                    "{\n"
                    "    return false;\n"
                    "}"
                )

            content = re.sub(
                r"// --- \[PYTHON-MANAGED RANGE CHECK START\] ---.*?// --- \[PYTHON-MANAGED RANGE CHECK END\] ---",
                range_block,
                content,
                flags=re.DOTALL,
            )

            apply_code, apply_warning = self._generate_uv_apply_code(attributes, stride)
            if apply_code is None:
                print(f"更新UV偏移着色器失败: {apply_warning}")
                return False
            if apply_warning:
                print(f"[UVOffset] {apply_warning}")

            content = re.sub(
                r"// --- \[PYTHON-MANAGED APPLY START\] ---.*?// --- \[PYTHON-MANAGED APPLY END\] ---",
                apply_code,
                content,
                flags=re.DOTALL,
            )

            with open(shader_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except Exception as e:
            print(f"更新UV偏移着色器文件失败: {e}")
            return False

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.scan_uv_offset_variables", text="预分配UV偏移变量", icon='FILE_REFRESH').node_name = self.name
        if self.uv_offset_variable_items:
            box = layout.box()
            box.label(text=f"UV偏移变量映射 ({len(self.uv_offset_variable_items)})", icon='DRIVER')
            box.template_list(
                "SSMT_UL_UV_OFFSET_VARIABLE_MAPPINGS", "",
                self, "uv_offset_variable_items",
                self, "uv_offset_variable_index",
                rows=max(2, min(len(self.uv_offset_variable_items), 6)),
            )

        box = layout.box()
        box.label(text="UV偏移物体列表", icon='OBJECT_DATA')
        if not self.uv_objects:
            box.label(text="列表为空，请添加物体", icon='ERROR')
        else:
            box.label(text=f"共 {len(self.uv_objects)} 个物体", icon='INFO')

        for i, item in enumerate(self.uv_objects):
            row = box.row(align=True)
            label = str(item.object_name or "")
            if item.start_vertex >= 0 and item.end_vertex >= 0:
                label = f"{label} [{item.start_vertex}~{item.end_vertex}]"
            row.label(text=f"{i + 1}. {label}", icon='OBJECT_DATA')
            op_remove = row.operator("ssmt_postprocess.remove_uv_offset_object", text="", icon='X')
            op_remove.node_name = self.name
            op_remove.index = i

        box.separator()
        row = box.row(align=True)
        row.operator("ssmt_postprocess.add_uv_offset_object", text="添加选中物体", icon='ADD').node_name = self.name
        row.operator("ssmt_postprocess.clear_uv_offset_objects", text="一键清空", icon='TRASH').node_name = self.name

        prefixes = self._collect_uv_prefixes()
        if prefixes:
            box = layout.box()
            box.label(text="IB UV布局（工作空间自动解析）", icon='LINKED')
            for prefix in prefixes:
                attributes = self._get_workspace_uv_attributes(prefix)
                if attributes:
                    row = box.row(align=True)
                    row.label(
                        text=f"{prefix}: 工作空间布局 {uv_attributes_total_bytes(attributes)}B（{len(attributes)} 条属性）",
                        icon='CHECKBOX_HLT',
                    )
                else:
                    row = box.row(align=True)
                    row.label(text=f"{prefix}: 工作空间未解析，使用默认 20B 布局", icon='CHECKBOX_DEHLT')

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
        print(f"UV偏移后处理节点开始执行，Mod导出路径: {mod_export_path}")

        if not self.uv_objects or not any(str(item.object_name or "").strip() for item in self.uv_objects):
            print("UV偏移节点未配置任何物体，跳过")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("路径中未找到任何.ini文件")
            return
        target_ini_file = ini_files[0]

        shader_source_path = self._get_shader_source_path()
        if not shader_source_path or not os.path.exists(shader_source_path):
            print(f"UV偏移着色器模板未找到: {shader_source_path}")
            return

        self._create_cumulative_backup(target_ini_file, mod_export_path)

        try:
            sections, preserved_tail_content, preserved_driver_content = self._read_ini_to_ordered_dict(target_ini_file)
            if not sections:
                return

            self.ensure_uv_offset_variable_map(["X", "Y"])
            x_var, y_var = self.get_uv_offset_export_variable_names()

            draw_info = self._parse_ini_for_draw_info(sections, mod_export_path)
            if not draw_info:
                print("从INI中未解析到任何 mesh 绘制信息")
                return

            hash_to_ranges = defaultdict(list)
            hash_to_texcoord_resource = {}
            for item in self.uv_objects:
                obj_name = str(item.object_name or "").strip()
                if not obj_name:
                    continue
                obj_hash = self._extract_hash_from_name(obj_name)
                if not obj_hash:
                    print(f"[UVOffset] 无法从物体名称解析哈希: {obj_name}")
                    continue
                h_prefix = self._extract_hash_prefix(obj_hash)
                if not h_prefix:
                    continue
                resolved = self._resolve_object_range(obj_name, draw_info)
                if resolved is None:
                    print(f"[UVOffset] 未找到物体 '{obj_name}' 的顶点范围，跳过")
                    continue
                start_v, end_v = resolved
                item.start_vertex = start_v
                item.end_vertex = end_v
                hash_to_ranges[h_prefix].append((obj_name, start_v, end_v))
                if h_prefix not in hash_to_texcoord_resource:
                    texcoord_resource = self._find_texcoord_resource(sections, h_prefix)
                    if texcoord_resource:
                        hash_to_texcoord_resource[h_prefix] = texcoord_resource
                    else:
                        print(f"[UVOffset] 未找到哈希 {h_prefix} 的 Texcoord 资源，跳过")

            if not hash_to_ranges:
                print("没有解析到任何物体范围，跳过")
                return

            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            compute_blocks_to_add = OrderedDict()
            for h_prefix, ranges in hash_to_ranges.items():
                texcoord_resource = hash_to_texcoord_resource.get(h_prefix)
                if not texcoord_resource:
                    continue

                texcoord_path, stride = self._resolve_texcoord_file_info(sections, mod_export_path, texcoord_resource)
                if not texcoord_path:
                    print(f"[UVOffset] 未找到哈希 {h_prefix} 的 Texcoord 文件，跳过")
                    continue
                attributes = self._get_uv_attributes_for_prefix(h_prefix)
                if not attributes:
                    print(f"[UVOffset] 未配置哈希 {h_prefix} 的 UV 属性，跳过")
                    continue
                attributes_total = uv_attributes_total_bytes(attributes)
                if attributes_total != stride:
                    print(
                        f"[UVOffset] 哈希 {h_prefix} 的 UV属性布局总字节 {attributes_total} 与 Texcoord stride {stride} 不一致，跳过。"
                        f"（已优先尝试从工作空间解析该 IB 的 Texcoord 布局，若仍不一致请检查工作空间 SubmeshJson）"
                    )
                    continue
                if self._get_workspace_uv_attributes(h_prefix):
                    print(
                        f"[UVOffset] 哈希 {h_prefix} 已从工作空间解析 Texcoord 布局: "
                        f"{attributes_total} 字节, {len(attributes)} 条属性（动态适配，无需手动指定 UV 属性定义）"
                    )

                shader_dest_path = os.path.join(dest_res_dir, f"uv_offset_{h_prefix}.hlsl")
                shutil.copy2(shader_source_path, shader_dest_path)
                if not self._update_shader_file(shader_dest_path, [(r[1], r[2]) for r in ranges], attributes, stride):
                    print(f"更新哈希 {h_prefix} 的UV偏移着色器失败")
                    continue

                vertex_count = self._get_vertex_count(sections, h_prefix)
                if vertex_count is None:
                    vertex_count = os.path.getsize(texcoord_path) // max(1, stride)
                if not vertex_count:
                    print(f"[UVOffset] 无法确定哈希 {h_prefix} 的顶点数，跳过")
                    continue

                texcoord_section_lines = sections.get(f"[{texcoord_resource}]", []) or []
                texcoord_filename = None
                for line in texcoord_section_lines:
                    value = self._extract_ini_assignment_value(line, "filename")
                    if value:
                        texcoord_filename = value
                        break
                if not texcoord_filename:
                    print(f"[UVOffset] 未找到哈希 {h_prefix} 的 Texcoord filename，跳过")
                    continue

                # raw uint 视图：同一 texcoord 文件以 stride=4 声明，compute 按 uint 数组读写；
                # 绘制仍用原 Texcoord 资源（stride=20 的 VB view）引用改写后的 buffer
                raw_resource = f"Resource{h_prefix}TexcoordRaw"
                raw_section_name = f"[{raw_resource}]"
                if raw_section_name not in sections:
                    sections[raw_section_name] = [
                        "type = Buffer",
                        "stride = 4",
                        f"filename = {texcoord_filename}",
                    ]

                ensure_resource_alias_section(
                    sections,
                    texcoord_resource,
                    "_0",
                    source_candidates=[texcoord_resource],
                )
                block_name = f"[CustomShader_{h_prefix}_UVOffset]"
                block_lines = [
                    "\n    ; --- Shared UV Offset Controls ---",
                    f"    x100 = {x_var} \n; Offset X",
                    f"    x101 = {y_var} \n; Offset Y",
                    f"\n    cs = ./res/uv_offset_{h_prefix}.hlsl",
                    f"    cs-u5 = copy {raw_resource}",
                    f"    {texcoord_resource} = ref cs-u5",
                ]
                dispatch_count = self._compute_dispatch_group_count(vertex_count, threads_per_group=16)
                block_lines.extend([
                    f"    Dispatch = {dispatch_count}, 1, 1",
                    "    cs-u5 = null",
                ])
                compute_blocks_to_add[block_name] = block_lines

            if not compute_blocks_to_add:
                print("没有生成任何UV偏移计算块，跳过")
                return

            # 清理失效的 UVOffset 计算块与 run 行，保持幂等
            for section_name in list(sections.keys()):
                if re.match(r'^\[CustomShader_.*_UVOffset\]$', section_name) and section_name not in compute_blocks_to_add:
                    del sections[section_name]

            active_hashes = [
                h_prefix
                for h_prefix in hash_to_ranges
                if f"[CustomShader_{h_prefix}_UVOffset]" in compute_blocks_to_add
            ]

            constants_section = "[Constants]"
            if constants_section not in sections:
                sections[constants_section] = []
            constants_lines = sections[constants_section]
            constants_content = "\n".join(constants_lines)

            new_constant_lines = []
            for var in (x_var, y_var):
                if f"global persist {var}" not in constants_content and f"global {var}" not in constants_content:
                    new_constant_lines.append(f"global persist {var} = 0.0")
            if new_constant_lines:
                constants_lines.append("\n; --- Auto-generated UV Offset Controls ---")
                constants_lines.extend(new_constant_lines)

            for h_prefix in active_hashes:
                texcoord_resource = hash_to_texcoord_resource[h_prefix]
                ensure_resource_alias_section(
                    sections,
                    texcoord_resource,
                    "_0",
                    source_candidates=[texcoord_resource],
                )
                post_copy = f"post {texcoord_resource} = copy_desc {texcoord_resource}_0"
                if post_copy not in constants_content:
                    constants_lines.append(post_copy)
                post_run = f"post run = CustomShader_{h_prefix}_UVOffset"
                if post_run not in constants_content:
                    constants_lines.append(post_run)

            active_post_runs = {f"post run = CustomShader_{h}_UVOffset" for h in active_hashes}
            constants_lines[:] = [
                line
                for line in constants_lines
                if not (
                    line.strip().startswith("post run = CustomShader_")
                    and line.strip().endswith("_UVOffset")
                    and line.strip() not in active_post_runs
                )
            ]
            sections[constants_section] = constants_lines

            present_section = "[Present]"
            if present_section not in sections:
                sections[present_section] = []
            present_lines = sections[present_section]
            active_run_lines = {f"    run = CustomShader_{h}_UVOffset" for h in active_hashes}
            present_lines[:] = [
                line
                for line in present_lines
                if not (
                    line.strip().startswith("run = CustomShader_")
                    and line.strip().endswith("_UVOffset")
                    and line.strip() not in active_run_lines
                )
            ]
            for h_prefix in active_hashes:
                run_line = f"    run = CustomShader_{h_prefix}_UVOffset"
                if run_line not in present_lines:
                    present_lines.append(run_line)
            sections[present_section] = present_lines

            sections.update(compute_blocks_to_add)
            self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)
            print(f"UV偏移配置已生成: {len(active_hashes)} 个哈希，{sum(len(v) for v in hash_to_ranges.values())} 个物体范围")
        except Exception as e:
            print(f"生成UV偏移配置时发生未知错误: {e}")
            import traceback
            traceback.print_exc()
            raise

        print("UV偏移后处理节点执行完成")


class SSMT_OT_PostProcess_AddUVOffsetObject(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.add_uv_offset_object"
    bl_label = "添加选中物体"
    bl_description = "将当前选中的网格物体添加到UV偏移物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_UVOffset':
            self.report({'ERROR'}, "请选择UV偏移节点")
            return {'CANCELLED'}

        objects = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == 'MESH']
        if not objects and getattr(context, "active_object", None) is not None:
            objects = [context.active_object]
        if not objects:
            self.report({'WARNING'}, "请先选中要添加的物体")
            return {'CANCELLED'}

        existing = {str(item.object_name or "") for item in node.uv_objects}
        added_count = 0
        for obj in objects:
            if obj.name in existing:
                continue
            item = node.uv_objects.add()
            item.object_name = obj.name
            existing.add(obj.name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个物体")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveUVOffsetObject(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.remove_uv_offset_object"
    bl_label = "删除物体"
    bl_description = "从UV偏移物体列表中删除选中的物体"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_UVOffset':
            self.report({'ERROR'}, "请选择UV偏移节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.uv_objects):
            node.uv_objects.remove(self.index)
        return {'FINISHED'}


class SSMT_OT_PostProcess_ClearUVOffsetObjects(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.clear_uv_offset_objects"
    bl_label = "一键清空"
    bl_description = "清空UV偏移物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_UVOffset':
            self.report({'ERROR'}, "请选择UV偏移节点")
            return {'CANCELLED'}

        while len(node.uv_objects) > 0:
            node.uv_objects.remove(len(node.uv_objects) - 1)
        self.report({'INFO'}, "已清空UV偏移物体列表")
        return {'FINISHED'}


class SSMT_OT_ScanUVOffsetVariables(bpy.types.Operator):
    bl_idname = "ssmt.scan_uv_offset_variables"
    bl_label = "预分配UV偏移变量"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or node.bl_idname != 'SSMTNode_PostProcess_UVOffset':
            self.report({'WARNING'}, "未找到UV偏移节点")
            return {'CANCELLED'}

        created_count, backfilled_count = node.ensure_uv_offset_variable_map(["X", "Y"])
        x_var, y_var = node.get_uv_offset_export_variable_names()
        self.report(
            {'INFO'},
            f"已预分配UV偏移变量: X={x_var}, Y={y_var}（新增 {created_count}，回填 {backfilled_count}）",
        )
        return {'FINISHED'}


classes = (
    UVOffsetVariableItem,
    SSMT_UL_UV_OFFSET_VARIABLE_MAPPINGS,
    UVOffsetObjectItem,
    SSMTNode_PostProcess_UVOffset,
    SSMT_OT_PostProcess_AddUVOffsetObject,
    SSMT_OT_PostProcess_RemoveUVOffsetObject,
    SSMT_OT_PostProcess_ClearUVOffsetObjects,
    SSMT_OT_ScanUVOffsetVariables,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
