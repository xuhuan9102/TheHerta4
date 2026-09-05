from __future__ import annotations

from collections import OrderedDict
import glob
import importlib
import json
import os
from pathlib import Path
import re

import bpy
import numpy as np

from .direct_export_shapekey import DirectShapeKeyGenerator
from .direct_export_shapekey_shared import ShapeKeyDirectExportError
from ..common.d3d11_gametype import D3D11GameType
from ..utils.log_utils import LOG
from .ntmi_layout_adapter import (
    iter_name_variants,
    local_loop_indices_for_export_range,
    parse_ntmi_part_layouts,
)
from ..ui.ntmi_modimp.modimp_core import ensure_mod_importer_package
from ..ui.ntmi_modimp.ntemi_importer import _ensure_ntemi_game_data_converter


class NTMIShapeKeyNodeAdapter:
    INTENSITY_START_INDEX = 100
    VERTEX_RANGE_START_INDEX = 200

    def __init__(self, original_node, sections, mod_export_path: str, ini_path: str):
        self.original_node = original_node
        self.sections = sections
        self.mod_export_path = mod_export_path
        self.ini_path = ini_path
        self.part_layouts = parse_ntmi_part_layouts(
            sections,
            output_dir=mod_export_path,
            source_ini_path=ini_path,
        )
        self._object_to_part_token: dict[str, str] = {}
        self._populate_draw_ranges()
        self._register_name_variants()

        # NTMI needs a deterministic pre-skin packed-delta path so the generated
        # compute stage can feed the core skinning command list correctly.
        self.use_packed_Meshess = True
        self.store_deltas = True
        self.use_optimized_lookup = True
        self.merge_slot_files = True

    def _populate_draw_ranges(self):
        for part_layout in self.part_layouts.values():
            vertex_cursor = 0
            for draw_call in part_layout.draw_calls:
                if draw_call.vertex_count:
                    start_vertex = vertex_cursor
                    end_vertex = vertex_cursor + int(draw_call.vertex_count) - 1
                    vertex_cursor = end_vertex + 1
                else:
                    start_vertex, end_vertex = self.original_node._calculate_vertex_range(
                        draw_call.ib_path,
                        draw_call.draw_params,
                    )
                    if start_vertex is not None and end_vertex is not None:
                        vertex_cursor = max(vertex_cursor, int(end_vertex) + 1)
                draw_call.start_vertex = start_vertex
                draw_call.end_vertex = end_vertex

    def _register_name_variants(self):
        for part_token, part_layout in self.part_layouts.items():
            for draw_call in part_layout.draw_calls:
                for candidate_name in iter_name_variants(draw_call.mesh_name):
                    self._object_to_part_token.setdefault(candidate_name, part_token)

    def get_part_layout(self, part_token: str):
        return self.part_layouts.get(str(part_token or "").strip())

    def get_draw_info_map(self):
        draw_info_map = {}
        for part_layout in self.part_layouts.values():
            for draw_call in part_layout.draw_calls:
                draw_info_map.setdefault(draw_call.mesh_name, []).append(
                    {
                        "draw_params": draw_call.draw_params,
                        "ib_path": draw_call.ib_path,
                    }
                )
        return draw_info_map

    def _extract_hash_from_name(self, obj_name):
        clean_name = str(obj_name or "").strip()
        if not clean_name:
            return None
        if clean_name in self._object_to_part_token:
            return self._object_to_part_token[clean_name]
        for candidate_name in iter_name_variants(clean_name):
            part_token = self._object_to_part_token.get(candidate_name)
            if part_token:
                return part_token
        if len(self.part_layouts) == 1:
            return next(iter(self.part_layouts.keys()))
        return None

    def _extract_hash_prefix(self, hash_val):
        return str(hash_val or "").strip() or None

    def _hash_to_resource_prefix(self, value):
        return self.original_node._create_safe_var_name(str(value or "").replace("-", "_"))

    def _extract_alias_from_name(self, obj_name):
        return self.original_node._extract_alias_from_name(obj_name)

    def _strip_runtime_copy_suffix(self, name):
        return self.original_node._strip_runtime_copy_suffix(name)

    def _strip_object_suffix(self, name):
        return self.original_node._strip_object_suffix(name)

    def _get_merge_identity_alias(self, obj_name):
        return self.original_node._get_merge_identity_alias(obj_name)

    def _create_safe_var_name(self, text, prefix="", existing_names=None):
        return self.original_node._create_safe_var_name(text, prefix=prefix, existing_names=existing_names)

    def _should_merge_slot_files(self, use_packed=None):
        return self.original_node._should_merge_slot_files(use_packed)

    def _get_vertex_struct_definition(self, hash_val=None):
        del hash_val
        return "struct VertexAttributes {\n    float3 position;\n};"

    def parse_vertex_struct(self, struct_definition):
        return self.original_node.parse_vertex_struct(struct_definition)

    def _detect_vertex_format(self, base_bytes, shapekey_bytes, struct_definition=None, **kwargs):
        return self.original_node._detect_vertex_format(base_bytes, shapekey_bytes, struct_definition, **kwargs)

    def _get_shader_source_path(self):
        return self.original_node._get_shader_source_path()

    def _create_cumulative_backup(self, ini_file_path, mod_export_path):
        return self.original_node._create_cumulative_backup(ini_file_path, mod_export_path)

    def _read_ini_to_ordered_dict(self, ini_file_path):
        return self.original_node._read_ini_to_ordered_dict(ini_file_path)

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content=""):
        return self.original_node._write_ordered_dict_to_ini(sections, ini_file_path, preserved_tail_content, preserved_driver_content)

    def _calculate_vertex_range(self, ib_path, draw_params):
        normalized_ib_path = os.path.normcase(os.path.normpath(str(ib_path or "")))
        normalized_draw_params = tuple(int(value) for value in tuple(draw_params or ()))
        for part_layout in self.part_layouts.values():
            for draw_call in part_layout.draw_calls:
                if (
                    os.path.normcase(os.path.normpath(draw_call.ib_path)) == normalized_ib_path
                    and tuple(int(value) for value in draw_call.draw_params) == normalized_draw_params
                    and draw_call.start_vertex is not None
                    and draw_call.end_vertex is not None
                ):
                    return draw_call.start_vertex, draw_call.end_vertex
        return self.original_node._calculate_vertex_range(ib_path, draw_params)

    def _get_merged_data_file_suffix(self, use_delta):
        return self.original_node._get_merged_data_file_suffix(use_delta)

    def _compute_dispatch_group_count(self, vertex_count, threads_per_group=16):
        return self.original_node._compute_dispatch_group_count(
            vertex_count,
            threads_per_group=threads_per_group,
        )

    def get_shape_key_export_variable_name(self, shape_key_name: str) -> str:
        return self.original_node.get_shape_key_export_variable_name(shape_key_name)

    @property
    def drag_drive_enabled(self) -> bool:
        return bool(getattr(self.original_node, "drag_drive_enabled", False))

    @property
    def DRAG_DRIVE_REGISTER(self) -> int:
        return int(getattr(self.original_node, "DRAG_DRIVE_REGISTER", 100))

    @property
    def DRAG_CLICK_COUNT_REGISTER(self) -> int:
        return int(getattr(self.original_node, "DRAG_CLICK_COUNT_REGISTER", 101))

    def _drag_shapekey_drive_resource_name(self, ini_path=None):
        return self.original_node._drag_shapekey_drive_resource_name(ini_path)

    def _drag_shapekey_click_count_resource_name(self, ini_path=None):
        return self.original_node._drag_shapekey_click_count_resource_name(ini_path)

    def _drag_drive_zone_ids(self, unique_names):
        return self.original_node._drag_drive_zone_ids(unique_names)

    def _drag_drive_click_stages(self, unique_names):
        return self.original_node._drag_drive_click_stages(unique_names)

    def _drag_drive_stage_count(self):
        return self.original_node._drag_drive_stage_count()

    def _drag_drive_buffer_layout(self):
        return self.original_node._drag_drive_buffer_layout()

    def _drag_drive_dirs(self, unique_names):
        return self.original_node._drag_drive_dirs(unique_names)

    def _update_shader_file(
        self,
        shader_path,
        hash_slot_data,
        use_packed,
        use_delta,
        unique_names,
        unique_objects,
        use_optimized=False,
        merge_slot_files=False,
        drag_drive_enabled=False,
        drag_zone_ids=None,
        drag_click_stages=None,
        drag_stage_count=1,
        drag_dirs=None,
        hash_val=None,
    ):
        del unique_objects, use_packed, use_delta, use_optimized, merge_slot_files, hash_val
        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0
        zone_ids = list(drag_zone_ids or []) if drag_drive_enabled else []
        click_stages = list(drag_click_stages or []) if drag_drive_enabled else []
        dirs = list(drag_dirs or []) if drag_drive_enabled else []
        # 按区域独立段布局：每区域 4 方向槽 + 该区域档位数 N 个无方向槽
        _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
        zone_bases = list(_zone_bases)
        zone_stage_counts = list(_zone_stage_counts)
        drive_extra_lines = []
        if drag_drive_enabled:
            drive_extra_lines.append(f"Buffer<float> ShapeKeyDrive : register(t{self.DRAG_DRIVE_REGISTER});")
            drive_extra_lines.append(f"Buffer<uint> ShapeKeyClickCount : register(t{self.DRAG_CLICK_COUNT_REGISTER});")
            if any(zone >= 0 for zone in zone_ids):
                ids_text = ", ".join(str(zone) if zone >= 0 else "0xFFFFFFFFu" for zone in zone_ids)
                drive_extra_lines.append(f"static const uint SHAPEKEY_ZONE_IDS[{len(zone_ids)}] = {{ {ids_text} }};")
                drive_extra_lines.append(f"static const uint SHAPEKEY_ZONE_IDS_COUNT = {len(zone_ids)}u;")
                dir_list = list(dirs) if len(dirs) == len(zone_ids) else [4] * len(zone_ids)
                stage_list = list(click_stages) if len(click_stages) == len(zone_ids) else [1] * len(zone_ids)
                nd_stage_ids = []
                slot_ids = []
                for idx, zone in enumerate(zone_ids):
                    if zone < 0 or zone >= len(zone_bases):
                        nd_stage_ids.append(0xFFFFFFFF)
                        slot_ids.append(0xFFFFFFFF)
                        continue
                    d = dir_list[idx] if idx < len(dir_list) else 4
                    if d >= 0 and d < 4:
                        nd_stage_ids.append(0xFFFFFFFF)
                        slot_ids.append(zone_bases[zone] + d)
                    else:
                        stage = max(1, stage_list[idx] if idx < len(stage_list) else 1)
                        nd_stage_ids.append(stage)
                        slot_ids.append(zone_bases[zone] + 4 + (stage - 1))
                nd_text = ", ".join(str(v) if v >= 0 else "0xFFFFFFFFu" for v in nd_stage_ids)
                drive_extra_lines.append(f"static const uint SHAPEKEY_ND_STAGE_IDS[{len(zone_ids)}] = {{ {nd_text} }};")
                slot_text = ", ".join(str(v) if v >= 0 else "0xFFFFFFFFu" for v in slot_ids)
                drive_extra_lines.append(f"static const uint SHAPEKEY_SLOT_IDS[{len(zone_ids)}] = {{ {slot_text} }};")
            else:
                drive_extra_lines.append("static const uint SHAPEKEY_ZONE_IDS[1] = { 0xFFFFFFFFu };")
                drive_extra_lines.append("static const uint SHAPEKEY_ZONE_IDS_COUNT = 1u;")
                drive_extra_lines.append("static const uint SHAPEKEY_ND_STAGE_IDS[1] = { 0xFFFFFFFFu };")
                drive_extra_lines.append("static const uint SHAPEKEY_SLOT_IDS[1] = { 0xFFFFFFFFu };")
            drive_extra_lines.append("")
        freq_define_lines = []
        for index, name in enumerate(unique_names):
            freq_define_lines.append(f"#define FREQ{index + 1} IniParams[{self.INTENSITY_START_INDEX + index}].x // {name}")
        if not freq_define_lines:
            freq_define_lines.append("// no shapekey params")

        shader_source = "\n".join(
            [
                "// NTMI pre-skin shapekey injector.",
                "// t51 = merged packed position deltas (float3)",
                "// t52 = merged vertex->packed index map",
                "// t53 = merged vertex->freq index map",
                "// t54 = original source position buffer (float triplets)",
                "// u5  = dynamic source position output (float triplets)",
                "",
                "StructuredBuffer<float3> merged_shapekey_pos_deltas : register(t51);",
                "StructuredBuffer<int> merged_shapekey_indices : register(t52);",
                "StructuredBuffer<uint> vertex_freq_indices : register(t53);",
                "Buffer<float> BasePosition : register(t54);",
                "RWBuffer<float> OutPosition : register(u5);",
                "Texture1D<float4> IniParams : register(t120);",
                *drive_extra_lines,
                "",
                "// Shape key parameter bindings",
                *freq_define_lines,
                "",
                "[numthreads(64, 1, 1)]",
                "void main(uint3 threadID : SV_DispatchThreadID)",
                "{",
                "    uint vertex_id = threadID.x;",
                "    uint position_float_count = 0u;",
                "    BasePosition.GetDimensions(position_float_count);",
                "    uint vertex_count = position_float_count / 3u;",
                "    if (vertex_id >= vertex_count)",
                "    {",
                "        return;",
                "    }",
                "",
                f"    uint num_slots = {int(num_slots)}u;",
                "    uint position_base = vertex_id * 3u;",
                "    float3 position_value = float3(",
                "        BasePosition[position_base + 0u],",
                "        BasePosition[position_base + 1u],",
                "        BasePosition[position_base + 2u]",
                "    );",
                "",
                "    if (num_slots > 0u)",
                "    {",
                "        for (uint slot_index = 0u; slot_index < num_slots; ++slot_index)",
                "        {",
                "            uint packed_idx = vertex_id * num_slots + slot_index;",
                "            uint freq_idx = vertex_freq_indices[packed_idx];",
                "            if (freq_idx == 255u)",
                "            {",
                "                continue;",
                "            }",
                "            int delta_index = merged_shapekey_indices[packed_idx];",
                "            if (delta_index < 0)",
                "            {",
                "                continue;",
                "            }",
                "            float weight = IniParams[100u + freq_idx].x;",
                *(
                    [
                        "            if (freq_idx < SHAPEKEY_ZONE_IDS_COUNT && SHAPEKEY_ZONE_IDS[freq_idx] != 0xFFFFFFFFu && (SHAPEKEY_ND_STAGE_IDS[freq_idx] == 0xFFFFFFFFu || ShapeKeyClickCount[SHAPEKEY_ZONE_IDS[freq_idx]] == SHAPEKEY_ND_STAGE_IDS[freq_idx]))",
                        "            {",
                        "                weight = ShapeKeyDrive[SHAPEKEY_SLOT_IDS[freq_idx]];",
                        "            }",
                    ]
                    if drag_drive_enabled
                    else []
                ),
                "            if (abs(weight) <= 1.0e-6)",
                "            {",
                "                continue;",
                "            }",
                "            position_value += merged_shapekey_pos_deltas[delta_index] * weight;",
                "        }",
                "    }",
                "",
                "    OutPosition[position_base + 0u] = position_value.x;",
                "    OutPosition[position_base + 1u] = position_value.y;",
                "    OutPosition[position_base + 2u] = position_value.z;",
                "}",
            ]
        )
        Path(shader_path).write_text(shader_source, encoding="utf-8")
        return True

    def _parse_classification_text_final(self, text_content):
        slot_to_name_to_objects = OrderedDict()
        hash_to_objects = OrderedDict()
        all_objects = []

        current_slot = None
        current_shapekey_name = None
        for line in str(text_content or "").splitlines():
            stripped = str(line or "").strip()
            if not stripped or stripped.startswith("#"):
                continue

            slot_match = re.search(r"槽位\s*(\d+):", stripped)
            if slot_match:
                current_slot = int(slot_match.group(1))
                slot_to_name_to_objects.setdefault(current_slot, OrderedDict())
                current_shapekey_name = None
                continue

            name_match = re.search(r"名称:\s*(.+)", stripped)
            if name_match and current_slot is not None:
                current_shapekey_name = str(name_match.group(1) or "").strip()
                slot_to_name_to_objects[current_slot].setdefault(current_shapekey_name, [])
                continue

            obj_match = re.search(r"物体:\s*(.+)", stripped)
            if not obj_match or current_slot is None or current_shapekey_name is None:
                continue

            obj_name = str(obj_match.group(1) or "").strip()
            if obj_name not in slot_to_name_to_objects[current_slot][current_shapekey_name]:
                slot_to_name_to_objects[current_slot][current_shapekey_name].append(obj_name)
            if obj_name not in all_objects:
                all_objects.append(obj_name)

            logical_hash = self._extract_hash_from_name(obj_name)
            if logical_hash:
                hash_to_objects.setdefault(logical_hash, [])
                if obj_name not in hash_to_objects[logical_hash]:
                    hash_to_objects[logical_hash].append(obj_name)

        unique_hashes = list(
            OrderedDict.fromkeys(
                logical_hash
                for obj_name in all_objects
                if (logical_hash := self._extract_hash_from_name(obj_name))
            )
        )
        return slot_to_name_to_objects, unique_hashes, hash_to_objects, all_objects

    def _parse_ini_for_draw_info(self, sections, base_path):
        del sections, base_path
        return self.get_draw_info_map()


def _position_format_from_stride(position_stride: int) -> str:
    if int(position_stride) == 16:
        return "R32G32B32A32_FLOAT"
    if int(position_stride) == 8:
        return "R16G16B16A16_FLOAT"
    return "R32G32B32_FLOAT"


def _build_minimal_position_game_type(position_stride: int) -> D3D11GameType:
    return D3D11GameType.from_submesh_json_dict(
        {
            "WorkGameType": "NTMI_DIRECT",
            "GPU-PreSkinning": False,
            "CategoryDrawCategoryMap": {"Position": "Position"},
        },
        override_d3d11_element_list=[
            {
                "SemanticName": "POSITION",
                "SemanticIndex": 0,
                "Format": _position_format_from_stride(position_stride),
                "ByteWidth": int(position_stride),
                "ExtractSlot": "vb0",
                "ExtractTechnique": "trianglelist",
                "Category": "Position",
            }
        ],
    )


def _load_ntmi_exporter_module(configured_root: str = ""):
    package = ensure_mod_importer_package(configured_root)
    return importlib.import_module(f"{package.__name__}.core.exporter")


def _build_exported_loop_indices(
    obj,
    *,
    exporter_module,
    flip_uv_v: bool = False,
    default_mirror_flip: bool = False,
) -> np.ndarray:
    if obj is None or getattr(obj, "type", "") != "MESH":
        return np.asarray([], dtype=np.int32)

    depsgraph = bpy.context.evaluated_depsgraph_get()
    mirror_flip = exporter_module._resolve_mirror_flip_for_object(
        obj,
        default_value=default_mirror_flip,
    )
    mesh_copy, _ = exporter_module._triangulated_mesh_copy(obj, depsgraph=depsgraph)
    try:
        uv0_layer, _ = exporter_module._export_uv0_layer(mesh_copy)
        if uv0_layer is None:
            return np.asarray([], dtype=np.int32)

        uv1_layer = exporter_module._find_uv_layer(mesh_copy, "UV1")
        uv3_layer = exporter_module._find_uv_layer(mesh_copy, "UV3", "packed_uv2")
        uv4_layer = exporter_module._find_uv_layer(mesh_copy, "UV4", "packed_uv3")
        packed_uv2, _ = exporter_module._optional_point_vector_attribute(mesh_copy, "packed_uv2")
        packed_uv3, _ = exporter_module._optional_point_vector_attribute(mesh_copy, "packed_uv3")
        loop_frames = exporter_module._prepare_loop_tangent_frames(mesh_copy, uv_layer_name=uv0_layer.name)

        def _uv_key(uv_pair: tuple[float, float]) -> tuple[int, int]:
            return (
                int(round(float(uv_pair[0]) * 1_000_000.0)),
                int(round(float(uv_pair[1]) * 1_000_000.0)),
            )

        def _to_game_uv_pair(uv_pair: tuple[float, float]) -> tuple[float, float]:
            u_coord, v_coord = uv_pair
            return (float(u_coord), 1.0 - float(v_coord) if flip_uv_v else float(v_coord))

        def _loop_uv_pair(
            loop_index: int,
            source_vertex_index: int,
            loop_uv_layer,
            fallback_values,
        ) -> tuple[float, float]:
            if loop_uv_layer is not None:
                uv_value = loop_uv_layer.data[loop_index].uv
                return (float(uv_value[0]), float(uv_value[1]))
            fallback = fallback_values[source_vertex_index]
            return (float(fallback[0]), float(fallback[1]))

        remap = {}
        local_loop_indices = []
        for polygon in mesh_copy.polygons:
            if polygon.loop_total != 3:
                continue
            for loop_index in polygon.loop_indices:
                source_vertex_index = mesh_copy.loops[loop_index].vertex_index
                uv0 = _to_game_uv_pair(tuple(float(value) for value in uv0_layer.data[loop_index].uv))
                uv1 = uv0 if uv1_layer is None else _to_game_uv_pair(
                    tuple(float(value) for value in uv1_layer.data[loop_index].uv)
                )
                uv2 = _to_game_uv_pair(_loop_uv_pair(loop_index, source_vertex_index, uv3_layer, packed_uv2))
                uv3 = _to_game_uv_pair(_loop_uv_pair(loop_index, source_vertex_index, uv4_layer, packed_uv3))

                if loop_frames is not None:
                    loop_tangents, loop_normals, loop_signs = loop_frames
                    decoded_tangent = loop_tangents[loop_index]
                    decoded_normal = loop_normals[loop_index]
                    decoded_sign = loop_signs[loop_index]
                else:
                    decoded_tangent, decoded_normal, decoded_sign = exporter_module._fallback_vertex_frame(
                        mesh_copy,
                        source_vertex_index,
                    )

                if mirror_flip:
                    decoded_tangent = exporter_module._mirror_x_vector(decoded_tangent)
                    decoded_normal = exporter_module._mirror_x_vector(decoded_normal)
                    decoded_sign = -decoded_sign

                key = (
                    source_vertex_index,
                    *_uv_key(uv0),
                    *_uv_key(uv1),
                    *_uv_key(uv2),
                    *_uv_key(uv3),
                    *exporter_module._vector_key(decoded_normal),
                    *exporter_module._vector_key(decoded_tangent),
                    int(decoded_sign >= 0.0),
                )
                if key in remap:
                    continue

                remap[key] = len(local_loop_indices)
                local_loop_indices.append(int(loop_index))

        return np.asarray(local_loop_indices, dtype=np.int32)
    finally:
        bpy.data.meshes.remove(mesh_copy)


def _resolve_ntmi_profile_id(mod_export_path: str) -> str:
    report_candidates = sorted(Path(mod_export_path).glob("*ntmi_modimp_export_report.json"))
    for report_path in report_candidates:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        export_results = payload.get("export_results") or []
        if not export_results:
            continue
        profile_id = str(export_results[0].get("profile_id", "") or "").strip()
        if profile_id:
            return profile_id
    return "yihuan"


def _load_ntmi_position_converter(mod_export_path: str, configured_root: str = ""):
    package = ensure_mod_importer_package(configured_root)
    game_data_module = importlib.import_module(f"{package.__name__}.core.game_data")
    _ensure_ntemi_game_data_converter(configured_root)
    profile_id = _resolve_ntmi_profile_id(mod_export_path)
    converter = game_data_module.get_game_data_converter(profile_id)
    LOG.info(f"NTMI ShapeKey: using game-data converter profile '{profile_id}' for sampled position overrides")
    return converter


class NTMIDirectShapeKeyGenerator(DirectShapeKeyGenerator):
    def __init__(self, node, mod_export_path: str, blueprint_model, exporter):
        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            raise ShapeKeyDirectExportError("NTMI ShapeKey: no ini file was found in the output directory.")

        self.original_node = node
        self.target_ini_file = ini_files[0]
        sections, preserved_tail_content, preserved_driver_content = node._read_ini_to_ordered_dict(self.target_ini_file)

        adapter_node = NTMIShapeKeyNodeAdapter(
            original_node=node,
            sections=sections,
            mod_export_path=mod_export_path,
            ini_path=self.target_ini_file,
        )
        self.adapter_node = adapter_node
        self._sections = sections
        self._preserved_tail_content = preserved_tail_content
        self._preserved_driver_content = preserved_driver_content
        self.mod_importer_root = str(getattr(exporter, "mod_importer_root", "") or "").strip()
        self._modimp_exporter_module = _load_ntmi_exporter_module(self.mod_importer_root)
        self._position_converter = _load_ntmi_position_converter(mod_export_path, self.mod_importer_root)

        super().__init__(adapter_node, mod_export_path, blueprint_model, exporter)
        self.meshes_dir = os.path.join(mod_export_path, "Buffer")

    def _convert_position_coords_for_export(self, sampled_coords):
        coords = np.asarray(sampled_coords, dtype=np.float32)
        if coords.size > 0:
            converted = np.empty_like(coords, dtype=np.float32)
            for index, (x_value, y_value, z_value) in enumerate(coords.tolist()):
                converted[index] = self._position_converter.from_blender_position(
                    (float(x_value), float(y_value), float(z_value))
                )
            coords = converted
        return coords

    def _convert_position_deltas_for_export(self, sampled_deltas):
        return self._convert_position_coords_for_export(sampled_deltas)

    def _match_drawib_model(self, actual_hash: str, logical_hash: str):
        del actual_hash, logical_hash
        return None

    def _build_part_object_export_context_lookup(self, part_layout, d3d11_game_type):
        context_lookup = {}
        loop_index_cache = {}
        for draw_call in part_layout.draw_calls:
            if draw_call.start_vertex is None or draw_call.end_vertex is None:
                continue
            export_indices = np.arange(draw_call.start_vertex, draw_call.end_vertex + 1, dtype=np.int32)
            mesh_obj = None
            for candidate_name in iter_name_variants(draw_call.mesh_name):
                mesh_obj = bpy.data.objects.get(candidate_name)
                if mesh_obj is not None:
                    break
            local_loop_indices = np.asarray([], dtype=np.int32)
            if mesh_obj is not None:
                cache_key = mesh_obj.name_full
                exported_loop_indices = loop_index_cache.get(cache_key)
                if exported_loop_indices is None:
                    exported_loop_indices = _build_exported_loop_indices(
                        mesh_obj,
                        exporter_module=self._modimp_exporter_module,
                        flip_uv_v=bool(getattr(self.exporter, "flip_uv_v", False)),
                        default_mirror_flip=bool(getattr(self.exporter, "default_mirror_flip", False)),
                    )
                    loop_index_cache[cache_key] = exported_loop_indices
                local_loop_indices = local_loop_indices_for_export_range(
                    exported_loop_indices,
                    export_indices,
                    draw_call.start_vertex,
                )
            context = {
                "export_indices": export_indices,
                "local_loop_indices": local_loop_indices,
                "d3d11_game_type": d3d11_game_type,
                "preferred_source_name": draw_call.mesh_name,
            }
            for candidate_name in iter_name_variants(draw_call.mesh_name):
                context_lookup.setdefault(candidate_name, context)
        return context_lookup

    def _build_runtime_infos(self, unique_hashes):
        runtime_infos = OrderedDict()
        for logical_hash in unique_hashes:
            part_layout = self.node.get_part_layout(logical_hash)
            if part_layout is None:
                LOG.warning(f"NTMI ShapeKey: skip unknown part token {logical_hash}")
                continue

            base_path = part_layout.position_path
            if not os.path.exists(base_path):
                LOG.warning(f"NTMI ShapeKey: missing base Position buffer {base_path}")
                continue

            with open(base_path, "rb") as file_obj:
                base_bytes = file_obj.read()

            total_draw_vertices = sum(
                int(draw_call.vertex_count)
                for draw_call in part_layout.draw_calls
                if draw_call.vertex_count
            )
            if total_draw_vertices > 0 and len(base_bytes) % total_draw_vertices == 0:
                position_stride = int(len(base_bytes) / total_draw_vertices)
                vertex_count = total_draw_vertices
            else:
                hinted_vertex_count = next(
                    (
                        int(draw_call.vertex_count)
                        for draw_call in part_layout.draw_calls
                        if draw_call.vertex_count
                    ),
                    0,
                )
                if hinted_vertex_count <= 0 and part_layout.draw_calls:
                    first_draw = part_layout.draw_calls[0]
                    if first_draw.start_vertex is not None and first_draw.end_vertex is not None:
                        hinted_vertex_count = int(first_draw.end_vertex - first_draw.start_vertex + 1)

                if hinted_vertex_count > 0 and len(base_bytes) % hinted_vertex_count == 0:
                    position_stride = int(len(base_bytes) / hinted_vertex_count)
                    vertex_count = hinted_vertex_count
                else:
                    position_stride = 12 if len(base_bytes) % 12 == 0 else 16 if len(base_bytes) % 16 == 0 else 8
                    vertex_count = int(len(base_bytes) / position_stride) if position_stride > 0 else 0
            if vertex_count <= 0:
                LOG.warning(f"NTMI ShapeKey: invalid vertex count for {logical_hash}")
                continue

            d3d11_game_type = _build_minimal_position_game_type(position_stride)

            runtime_infos[logical_hash] = {
                "logical_hash": logical_hash,
                "actual_hash": f"{part_layout.file_stem}-directsk",
                "base_path": base_path,
                "base_bytes": base_bytes,
                "position_stride": position_stride,
                "vertex_count": vertex_count,
                "drawib_model": None,
                "part_layout": part_layout,
                "base_resource_name": part_layout.position_resource,
                "object_export_context_lookup": self._build_part_object_export_context_lookup(
                    part_layout,
                    d3d11_game_type,
                ),
            }

        if not runtime_infos:
            raise ShapeKeyDirectExportError("NTMI ShapeKey: no valid part Position buffers were found.")
        return runtime_infos

    def _build_runtime_infos_from_exporter_buffers(self, unique_hashes):
        del unique_hashes
        raise ShapeKeyDirectExportError(
            "NTMI ShapeKey: exporter-side shapekey buffers are not used on the NTMI path."
        )

    def _parse_hash_to_base_resources(self, sections):
        del sections
        return {
            part_token: [part_layout.position_resource]
            for part_token, part_layout in self.node.part_layouts.items()
        }

    def _ensure_present_run_lines(self, sections, shader_sections: list[str]):
        if "[Present]" not in sections:
            sections["[Present]"] = []
        present_lines = sections["[Present]"]
        guard_line = "if $active0 == 1"

        block_start = -1
        block_end = -1
        nested_if_depth = 0
        for index, line in enumerate(present_lines):
            stripped = str(line or "").strip()
            if block_start < 0:
                if stripped == guard_line:
                    block_start = index
                    nested_if_depth = 1
                continue
            if stripped.startswith("if "):
                nested_if_depth += 1
            elif stripped == "endif":
                nested_if_depth -= 1
                if nested_if_depth == 0:
                    block_end = index
                    break

        run_lines = [f"    run = {shader_section}" for shader_section in shader_sections]
        if block_start >= 0 and block_end >= 0:
            existing = {str(line or "").strip() for line in present_lines[block_start + 1:block_end]}
            insert_index = block_end
            for run_line in run_lines:
                if run_line.strip() in existing:
                    continue
                present_lines.insert(insert_index, run_line)
                insert_index += 1
            return

        if present_lines and str(present_lines[-1] or "").strip():
            present_lines.append("")
        present_lines.append(guard_line)
        present_lines.extend(run_lines)
        present_lines.append("endif")

    def _clone_base_position_resources(self, sections, part_tokens: list[str]):
        for part_token in part_tokens:
            part_layout = self.node.get_part_layout(part_token)
            if part_layout is None:
                continue
            base_section_name = f"[{part_layout.position_resource}]"
            alias_section_name = f"[{part_layout.position_resource}_Base]"
            if alias_section_name in sections:
                continue
            original_lines = list(sections.get(base_section_name, []))
            if not original_lines:
                original_lines = [
                    "type = Buffer",
                    "format = R32_FLOAT",
                    f"filename = Buffer/{Path(part_layout.position_path).name}",
                ]
            sections[alias_section_name] = original_lines

    def _patch_skin_commandlists(self, sections, unique_hashes, hash_to_vertex_count):
        part_token_set = {str(value) for value in unique_hashes}
        part_layout_map = {
            str(part_token): self.node.get_part_layout(part_token)
            for part_token in part_token_set
        }

        for section_name, lines in list(sections.items()):
            if not str(section_name or "").startswith("[CommandList_SkinParts_"):
                continue

            current_part_token = ""
            current_source_position_resource = ""
            patched_lines = []
            for line in lines:
                stripped = str(line or "").strip()

                if stripped.startswith("cs-t65 = ResourcePalette_"):
                    current_part_token = str(stripped.split("=", 1)[1] or "").strip().replace("ResourcePalette_", "")
                    part_layout = part_layout_map.get(current_part_token)
                    current_source_position_resource = (
                        str(getattr(part_layout, "position_resource", "") or "")
                        if part_layout is not None
                        else ""
                    )

                if stripped.startswith("cs-t68 = "):
                    current_source_position_resource = str(stripped.split("=", 1)[1] or "").strip()

                if stripped == "run = CommandList\\NTMIv1\\SkinFromBoundSlots" and current_part_token in part_token_set:
                    part_layout = part_layout_map.get(current_part_token)
                    if part_layout is not None:
                        vertex_count = int(hash_to_vertex_count.get(current_part_token, 0) or 0)
                        position_float_count = max(vertex_count * 3, 0)
                        shader_section_name = self._shapekey_shader_section_name(current_part_token)
                        direct_position_uav = f"ResourcePart_{current_part_token}_DirectShapeKey_Position_UAV"
                        direct_position = f"ResourcePart_{current_part_token}_DirectShapeKey_Position"
                        drive_node = getattr(self, "node", None)
                        drive_enabled = bool(
                            getattr(drive_node, "drag_drive_enabled", False)
                            if drive_node is not None
                            else getattr(self, "drag_drive_enabled", False)
                        )
                        drag_drive_resource = None
                        if drive_enabled and drive_node is not None:
                            try:
                                drag_drive_resource = drive_node._drag_shapekey_drive_resource_name(
                                    getattr(self, "target_ini_file", getattr(self, "ini_path", None))
                                )
                            except Exception:
                                drag_drive_resource = None
                        drive_register = (
                            int(getattr(drive_node, "DRAG_DRIVE_REGISTER", 100))
                            if drive_node is not None
                            else int(getattr(self, "DRAG_DRIVE_REGISTER", 100))
                        )
                        click_register = (
                            int(getattr(drive_node, "DRAG_CLICK_COUNT_REGISTER", 101))
                            if drive_node is not None
                            else int(getattr(self, "DRAG_CLICK_COUNT_REGISTER", 101))
                        )
                        drive_bind_lines = (
                            [
                                f"cs-t{drive_register} = {drag_drive_resource}",
                                f"cs-t{click_register} = {drive_node._drag_shapekey_click_count_resource_name(getattr(self, 'target_ini_file', getattr(self, 'ini_path', None)))}",
                            ]
                            if drag_drive_resource
                            else []
                        )
                        drive_unbind_lines = (
                            [f"cs-t{drive_register} = null", f"cs-t{click_register} = null"]
                            if drag_drive_resource
                            else []
                        )
                        patched_lines.extend(
                            [
                                f"cs-t51 = {self._shapekey_resource_name(current_part_token, 'Merged_PackedPosDelta')}",
                                f"cs-t52 = {self._shapekey_resource_name(current_part_token, 'Merged_Map')}",
                                f"cs-t53 = {self._shapekey_resource_name(current_part_token, 'FreqIndices')}",
                                f"cs-t54 = {current_source_position_resource or part_layout.position_resource}",
                                f"cs-u5 = {direct_position_uav}",
                                *drive_bind_lines,
                                f"run = {shader_section_name}",
                                f"{direct_position} = copy {direct_position_uav}",
                                "cs-t51 = null",
                                "cs-t52 = null",
                                "cs-t53 = null",
                                "cs-t54 = null",
                                "cs-u5 = null",
                                *drive_unbind_lines,
                                f"cs-t68 = {direct_position}",
                            ]
                        )
                    patched_lines.append(line)
                    continue

                if stripped.startswith("cs-t68 = ResourcePart_") and stripped.endswith("_Position") and current_part_token in part_token_set:
                    continue

                patched_lines.append(line)

            sections[section_name] = patched_lines

    def _shapekey_shader_section_name(self, part_token: str) -> str:
        return f"CustomShader_NTMI_ShapeKey_{self.node._hash_to_resource_prefix(part_token)}"

    def _shapekey_resource_name(self, part_token: str, suffix: str) -> str:
        return f"ResourcePart_{part_token}_DirectShapeKey_{suffix}"

    def _update_ini_sections(
        self,
        sections,
        preserved_tail_content,
        target_ini_file,
        slot_to_name_to_objects,
        unique_hashes,
        hash_to_objects,
        all_unique_names,
        all_unique_objects,
        calculated_ranges,
        hash_to_stride,
        hash_to_actual_file_hash,
        hash_to_vertex_count,
        hash_slot_data_map,
        hash_to_base_resources,
        use_packed,
        use_delta,
        use_optimized,
        merge_slot_files,
        preserved_driver_content="",
        drag_drive_resource=None,
    ):
        del slot_to_name_to_objects, all_unique_objects, hash_to_base_resources

        if "[Constants]" not in sections:
            sections["[Constants]"] = []
        constants_lines = sections["[Constants]"]
        constants_content = "".join(constants_lines)

        shapekey_freq_params = {
            name: self.node.get_shape_key_export_variable_name(name)
            for name in all_unique_names
        }

        intensity_lines = []
        for name, param in shapekey_freq_params.items():
            if param not in constants_content:
                intensity_lines.append(f"; Control shape key intensity for '{name}'")
                intensity_lines.append(f"global persist {param} = 0.0")
        if intensity_lines:
            constants_lines.append("\n; --- NTMI Auto-generated Shape Key Intensity Controls ---")
            constants_lines.extend(intensity_lines)

        vertex_range_vars = {}
        if not use_optimized:
            existing_vertex_range_names = set()
            vertex_range_lines = []
            for obj_name, range_tuple in calculated_ranges.items():
                start_v, end_v = range_tuple[:2]
                if start_v is None:
                    continue
                safe_name = self.node._create_safe_var_name(
                    obj_name.replace("-", "_"),
                    existing_names=existing_vertex_range_names,
                )
                start_var = f"$SV_{safe_name}"
                end_var = f"$EV_{safe_name}"
                vertex_range_vars[obj_name] = (start_var, end_var)
                if start_var not in constants_content:
                    vertex_range_lines.append(f"global {start_var} = {start_v}")
                if end_var not in constants_content:
                    vertex_range_lines.append(f"global {end_var} = {end_v}")
            if vertex_range_lines:
                constants_lines.append("\n; --- NTMI Auto-generated Vertex Ranges For Shape Keys ---")
                constants_lines.extend(vertex_range_lines)

        self._clone_base_position_resources(sections, list(unique_hashes))

        generated_resource_sections = OrderedDict()
        compute_sections = OrderedDict()

        for logical_hash in unique_hashes:
            actual_hash = hash_to_actual_file_hash.get(logical_hash)
            part_layout = self.node.get_part_layout(logical_hash)
            hash_slot_data = hash_slot_data_map.get(logical_hash, {})
            if actual_hash is None or part_layout is None or not hash_slot_data:
                continue

            shader_section_name = self._shapekey_shader_section_name(logical_hash)
            direct_position_uav = f"ResourcePart_{logical_hash}_DirectShapeKey_Position_UAV"
            direct_position = f"ResourcePart_{logical_hash}_DirectShapeKey_Position"
            position_float_count = int(hash_to_vertex_count.get(logical_hash, 0) or 0) * 3

            if merge_slot_files:
                data_resource_name = self._shapekey_resource_name(
                    logical_hash,
                    "Merged_PackedPosDelta" if use_delta else "Merged_Packed",
                )
                map_resource_name = self._shapekey_resource_name(logical_hash, "Merged_Map")
                generated_resource_sections[f"[{data_resource_name}]"] = [
                    "type = StructuredBuffer",
                    f"stride = {12 if use_delta else hash_to_stride.get(logical_hash, 40)}",
                    f"filename = Buffer/{actual_hash}-Position{self.node._get_merged_data_file_suffix(use_delta)}.buf",
                ]
                generated_resource_sections[f"[{map_resource_name}]"] = [
                    "type = StructuredBuffer",
                    "stride = 4",
                    f"filename = Buffer/{actual_hash}-Position_merged_map.buf",
                ]
            else:
                resource_suffix = "_PackedPosDelta" if use_packed and use_delta else "_PosDelta" if use_delta else "_Packed" if use_packed else ""
                file_suffix = "_packed_pos_delta" if use_packed and use_delta else "_pos_delta" if use_delta else "_packed" if use_packed else ""
                for slot_num in sorted(hash_slot_data.keys()):
                    generated_resource_sections[
                        f"[{self._shapekey_resource_name(logical_hash, f'Slot{slot_num:03d}{resource_suffix}')}]"
                    ] = [
                        "type = StructuredBuffer",
                        f"stride = {12 if use_delta else hash_to_stride.get(logical_hash, 40)}",
                        f"filename = Buffer/{actual_hash}-Position1{slot_num:03d}{file_suffix}.buf",
                    ]
                    if use_packed:
                        generated_resource_sections[
                            f"[{self._shapekey_resource_name(logical_hash, f'Slot{slot_num:03d}_Map')}]"
                        ] = [
                            "type = StructuredBuffer",
                            "stride = 4",
                            f"filename = Buffer/{actual_hash}-Position1{slot_num:03d}_map.buf",
                        ]

            if use_optimized:
                generated_resource_sections[f"[{self._shapekey_resource_name(logical_hash, 'FreqIndices')}]"] = [
                    "type = StructuredBuffer",
                    "stride = 4",
                    f"filename = Buffer/{actual_hash}-Position_freq_indices.buf",
                ]

            generated_resource_sections[f"[{direct_position_uav}]"] = [
                "dynamic_slots = 16",
                "type = RWBuffer",
                "format = R32_FLOAT",
                f"array = {position_float_count}",
            ]
            generated_resource_sections[f"[{direct_position}]"] = [
                "dynamic_slots = 16",
                "type = Buffer",
                "format = R32_FLOAT",
                f"array = {position_float_count}",
            ]

            hash_unique_names = list(
                OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys())
            )
            hash_unique_objects = list(
                OrderedDict.fromkeys(
                    obj_name
                    for slot_data in hash_slot_data.values()
                    for objects in slot_data.values()
                    for obj_name in objects
                    if obj_name in hash_to_objects.get(logical_hash, [])
                )
            )

            block_lines = ["\n    ; --- Shared Intensity Controls (per Shape Key Name) ---"]
            for index, name in enumerate(hash_unique_names):
                block_lines.append(
                    f"    x{self.node.INTENSITY_START_INDEX + index} = {shapekey_freq_params[name]} \n; {name}"
                )
            if not use_optimized:
                block_lines.append("\n    ; --- Per-Object Vertex Range Controls ---")
                for index, obj_name in enumerate(hash_unique_objects):
                    start_var, end_var = vertex_range_vars.get(obj_name, ("$SV_unknown", "$EV_unknown"))
                    block_lines.append(
                        f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2} = {start_var} \n; {obj_name} Start"
                    )
                    block_lines.append(
                        f"    x{self.node.VERTEX_RANGE_START_INDEX + index * 2 + 1} = {end_var} \n; {obj_name} End"
                    )

            block_lines.append(
                "\n    ; --- NTMI pre-skin ShapeKey injection ---"
            )
            block_lines.append(f"    cs = ./res/shapekey_anim_{logical_hash}.hlsl")
            dispatch_count = self.node._compute_dispatch_group_count(
                hash_to_vertex_count.get(logical_hash, 0),
                threads_per_group=64,
            )
            block_lines.append(f"    dispatch = {dispatch_count}, 1, 1")
            compute_sections[f"[{shader_section_name}]"] = block_lines

        self._patch_skin_commandlists(sections, unique_hashes, hash_to_vertex_count)
        for section_name, lines in generated_resource_sections.items():
            sections[section_name] = lines
        for section_name, lines in compute_sections.items():
            sections[section_name] = lines
        self.original_node._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)


def execute_ntmi_shapekey_postprocess(node, output_dir: str, blueprint_model, exporter):
    generator = NTMIDirectShapeKeyGenerator(
        node=node,
        mod_export_path=output_dir,
        blueprint_model=blueprint_model,
        exporter=exporter,
    )
    generator.generate()
