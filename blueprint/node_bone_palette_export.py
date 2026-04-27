import json
import os
import re
import shutil
import struct
import traceback
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import bpy

from ..utils.log_utils import LOG
from .node_base import SSMTNodeBase


BONESTORE_INI_FILE_NAME = "BoneStore.ini"
CAPTURE_MANIFEST_FILE_NAME = "capture_manifest.json"
EXPORT_MANIFEST_FILE_NAME = "export_manifest.json"
HLSL_EXPORT_DIR_NAME = "hlsl"
BUFFER_EXPORT_DIR_NAME = "Buffer"
BI4_MAX_BONE_INDEX = 255
BI4_MAX_BONE_COUNT = BI4_MAX_BONE_INDEX + 1

VERTEX_GROUP_STATE_PROP = "bmc_vertex_group_state"
VERTEX_GROUP_STATE_GLOBAL = "global"
VERTEX_GROUP_STATE_EXPORT_LOCAL = "export_local"
EXPORT_PALETTE_PROP = "bmc_export_palette_values"
EXPORT_CHUNK_PROP = "bmc_export_chunk"
GENERATED_NODE_PROP = "ssmt_bone_palette_export_node"
GENERATED_SOURCE_PROP = "ssmt_bone_palette_export_source"
GENERATED_STAGE_PROP = "ssmt_bone_palette_export_stage"

GENERATED_STAGE_PROCESSED = "processed"

_NUMERIC_GROUP_RE = re.compile(r"^\d+$")
_CHUNK_PREFIX_RE = re.compile(r"^(?P<hash>[0-9A-Fa-f]{8})[-_](?P<count>\d+)(?:[-_](?P<chunk>\d+))?")
_SAFE_NAMESPACE_RE = re.compile(r"[^0-9A-Za-z_]+")
_REQUIRED_HLSL_FILES = (
    "extract_cb1_vs.hlsl",
    "extract_cb1_ps.hlsl",
    "gather_bones_cs.hlsl",
    "record_bones_dynamic_cs.hlsl",
    "redirect_cb1_cs.hlsl",
)


@dataclass(frozen=True)
class MeshPrepareState:
    mesh_obj: object
    is_localized: bool
    localized_palette: tuple[int, ...]
    export_chunk: str
    used_global_groups: tuple[int, ...]


@dataclass(frozen=True)
class PartRecord:
    draw_index: int
    object_name: str
    vs_hash: str
    ib_hash: str
    match_index_count: int
    bone_count: int
    global_bone_base: int
    vb2_path: str
    vs_t0_path: str
    vs_cb1_path: str
    vs_cb1_first_constant: int
    vs_cb1_num_constants: int


@dataclass(frozen=True)
class LocalPaletteRecord:
    object_name: str
    ib_hash: str
    match_index_count: int
    chunk_index: int
    local_bone_count: int
    palette_values: tuple[int, ...]
    file_name: str
    file_path: str
    resource_suffix: str


class BonePaletteDebugLogger:
    def __init__(self, node_name: str):
        safe_name = re.sub(r"[^0-9A-Za-z_]+", "_", node_name or "Node").strip("_") or "Node"
        self.text_name = f"SSMT_BonePaletteExport_Debug_{safe_name}"
        self.lines = []

    def log(self, message: str) -> None:
        self.lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")
        LOG.info(f"[BonePaletteExport] {message}")

    def log_exception(self, error: Exception) -> None:
        self.lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR: {error}")
        LOG.error(f"[BonePaletteExport] {error}")
        for line in traceback.format_exc().splitlines():
            self.lines.append(line)
            LOG.error(line)

    def flush(self) -> str:
        text_block = bpy.data.texts.get(self.text_name)
        if text_block is None:
            text_block = bpy.data.texts.new(self.text_name)
        text_block.clear()
        text_block.write("\n".join(self.lines) + "\n")
        return text_block.name


def ensure_directory(path: str) -> str:
    normalized_path = os.path.abspath(path)
    os.makedirs(normalized_path, exist_ok=True)
    return normalized_path


def write_json(path: str, payload) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)
        file_handle.write("\n")
    return path


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def write_uint32_buffer(path: str, values) -> str:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as file_handle:
        for value in values:
            file_handle.write(struct.pack("<I", int(value)))
    return path


def build_bonestore_namespace(output_directory: str) -> str:
    normalized_output_directory = os.path.abspath(output_directory)
    base_name = os.path.basename(normalized_output_directory.rstrip("\\/")) or "BoneStore"
    safe_base_name = _SAFE_NAMESPACE_RE.sub("_", base_name).strip("_") or "BoneStore"
    checksum = zlib.crc32(normalized_output_directory.lower().encode("utf-8")) & 0xFFFFFFFF
    return f"BMC\\{safe_base_name}_{checksum:08x}"


def export_required_hlsl(output_directory: str) -> str:
    assets_dir = Path(__file__).resolve().parent.parent / "Toolset"
    if not assets_dir.exists():
        raise ValueError(f"未找到 Bone Merge Capture HLSL 资源目录: {assets_dir}")

    hlsl_output_dir = Path(output_directory).resolve() / HLSL_EXPORT_DIR_NAME
    hlsl_output_dir.mkdir(parents=True, exist_ok=True)

    for file_name in _REQUIRED_HLSL_FILES:
        source_path = assets_dir / file_name
        if not source_path.exists():
            raise ValueError(f"缺少必需的 HLSL 资源: {source_path}")
        shutil.copy2(source_path, hlsl_output_dir / file_name)

    return str(hlsl_output_dir)


def _deduplicate_part_records(part_records: list[PartRecord]) -> list[PartRecord]:
    unique_records = []
    seen_keys = set()
    for part_record in sorted(part_records, key=lambda item: (int(item.draw_index), int(item.global_bone_base))):
        key = (part_record.ib_hash.lower(), int(part_record.match_index_count))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_records.append(part_record)
    return unique_records


def _build_effective_palette_records(
    part_records: list[PartRecord],
    local_palette_records: list[LocalPaletteRecord],
) -> list[LocalPaletteRecord]:
    records_by_identity = {
        (record.ib_hash.lower(), int(record.match_index_count), int(record.chunk_index)): record
        for record in local_palette_records
    }
    effective_records = []
    used_keys = set()

    for part_record in part_records:
        key = (part_record.ib_hash.lower(), int(part_record.match_index_count), 0)
        record = records_by_identity.get(key)
        if record is None:
            palette_values = tuple(
                range(int(part_record.global_bone_base), int(part_record.global_bone_base) + int(part_record.bone_count))
            )
            record = LocalPaletteRecord(
                object_name=part_record.object_name,
                ib_hash=key[0],
                match_index_count=key[1],
                chunk_index=0,
                local_bone_count=len(palette_values),
                palette_values=palette_values,
                file_name=f"{key[0]}-{key[1]}-0-Palette.buf",
                file_path="",
                resource_suffix=f"{key[0]}_{key[1]}_0",
            )
        effective_records.append(record)
        used_keys.add(key)

    for key, record in sorted(records_by_identity.items(), key=lambda item: item[0]):
        if key in used_keys:
            continue
        effective_records.append(record)
    return effective_records


def _shader_override_sections(part_records: list[PartRecord]) -> list[str]:
    lines = []
    seen_hashes = set()
    for part_record in part_records:
        vs_hash = part_record.vs_hash.lower()
        if not vs_hash or vs_hash in seen_hashes:
            continue
        seen_hashes.add(vs_hash)
        lines.extend(
            [
                f"[ShaderOverrideBoneStoreVS_{vs_hash}]",
                f"hash = {vs_hash}",
                "filter_index = 200",
                "allow_duplicate_hash = overrule",
                "",
            ]
        )
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _shared_resource_sections() -> list[str]:
    return [
        "; -------------------------------------------------",
        "; Shared buffers",
        "; -------------------------------------------------",
        "[ResourceDumpedCB1_UAV]",
        "type = RWStructuredBuffer",
        "stride = 16",
        "array = 4096",
        "",
        "[ResourceDumpedCB1_SRV]",
        "type = Buffer",
        "stride = 16",
        "array = 4096",
        "",
        "[ResourceFakeCB1_UAV]",
        "type = RWStructuredBuffer",
        "stride = 16",
        "array = 4096",
        "",
        "[ResourceFakeCB1]",
        "type = Buffer",
        "stride = 16",
        "format = R32G32B32A32_UINT",
        "array = 4096",
        "",
        "[ResourceFakeT0_UAV]",
        "type = RWStructuredBuffer",
        "stride = 16",
        "array = 200000",
        "",
        "[ResourceFakeT0_SRV]",
        "type = StructuredBuffer",
        "stride = 16",
        "array = 200000",
        "",
        "[ResourceLocalFakeT0_UAV]",
        "type = RWStructuredBuffer",
        "stride = 16",
        "array = 4096",
        "",
        "[ResourceLocalFakeT0_SRV]",
        "type = StructuredBuffer",
        "stride = 16",
        "array = 4096",
        "",
        "; -------------------------------------------------",
        "; Stage 1 shaders: extract -> store into global FakeT0",
        "; -------------------------------------------------",
        "[CustomShader_ExtractCB1]",
        "vs = hlsl\\extract_cb1_vs.hlsl",
        "ps = hlsl\\extract_cb1_ps.hlsl",
        "ps-u7 = ResourceDumpedCB1_UAV",
        "depth_enable = false",
        "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
        "cull = none",
        "topology = point_list",
        "draw = 4096, 0",
        "ps-u7 = null",
        "ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV",
        "",
        "[CustomShader_RecordBones]",
        "cs = hlsl\\record_bones_dynamic_cs.hlsl",
        "cs-t0 = vs-t0",
        "cs-t1 = ResourceDumpedCB1_SRV",
        "; cs-t2 = ResourceBoneMeta_<ib>",
        "cs-u1 = ResourceFakeT0_UAV",
        "dispatch = 64, 1, 1",
        "cs-u1 = null",
        "cs-t0 = null",
        "cs-t1 = null",
        "cs-t2 = null",
        "ResourceFakeT0_SRV = copy ResourceFakeT0_UAV",
        "",
        "[CustomShader_GatherBones]",
        "cs = hlsl\\gather_bones_cs.hlsl",
        "cs-t0 = ResourceFakeT0_SRV",
        "cs-u1 = ResourceLocalFakeT0_UAV",
        "dispatch = 64, 1, 1",
        "cs-u1 = null",
        "cs-t0 = null",
        "cs-t2 = null",
        "cs-t3 = null",
        "ResourceLocalFakeT0_SRV = copy ResourceLocalFakeT0_UAV",
        "",
        "[CustomShader_RedirectCB1]",
        "cs = hlsl\\redirect_cb1_cs.hlsl",
        "cs-t0 = ResourceDumpedCB1_SRV",
        "; This is the only RedirectCB1 path now: it redirects cb1[5].x/.y",
        "; to the shared local gathered palette buffer, not the global buffer.",
        "cs-u0 = ResourceFakeCB1_UAV",
        "dispatch = 4, 1, 1",
        "cs-u0 = null",
        "cs-t0 = null",
        "ResourceFakeCB1 = copy ResourceFakeCB1_UAV",
        "",
        "; Local palette resources are generated per IB below.",
        "; ResourceBoneMeta_<ib>.y stores the original source IB bone count used by RecordBones.",
        "; ResourceLocalPaletteMeta_<ib>_<count>_0.x stores the local palette bone count used by GatherBones.",
        "; RedirectCB1 only needs the current consuming draw's cb1 as input; it rewrites",
        "; cb1[5].x/.y to point at the shared local palette buffer.",
    ]


def _local_palette_sections(local_palette_records: list[LocalPaletteRecord]) -> list[str]:
    if not local_palette_records:
        return []

    lines = [
        "; -------------------------------------------------",
        "; Local palettes generated during export preparation",
        "; -------------------------------------------------",
    ]
    seen_suffixes = set()
    for local_palette_record in local_palette_records:
        if local_palette_record.resource_suffix in seen_suffixes:
            continue
        seen_suffixes.add(local_palette_record.resource_suffix)
        lines.extend(
            [
                f"[ResourceLocalPalette_{local_palette_record.resource_suffix}]",
                "type = Buffer",
                "format = R32_UINT",
                f"filename = Buffer/{local_palette_record.file_name}",
                "",
                f"[ResourceLocalPaletteMeta_{local_palette_record.resource_suffix}]",
                "type = Buffer",
                "format = R32_FLOAT",
                f"data = {float(local_palette_record.local_bone_count):.1f}",
                "",
            ]
        )
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_default_part_palette(part_record: PartRecord, local_palette_record: LocalPaletteRecord) -> bool:
    expected_values = tuple(
        range(int(part_record.global_bone_base), int(part_record.global_bone_base) + int(part_record.bone_count))
    )
    return (
        local_palette_record.ib_hash.lower() == part_record.ib_hash.lower()
        and int(local_palette_record.match_index_count) == int(part_record.match_index_count)
        and int(local_palette_record.chunk_index) == 0
        and tuple(int(value) for value in local_palette_record.palette_values) == expected_values
    )


def _part_sections(part_record: PartRecord, local_palette_record: LocalPaletteRecord) -> list[str]:
    hash_value = part_record.ib_hash.lower()
    if _is_default_part_palette(part_record, local_palette_record):
        palette_comment = (
            "; Stage 2 palette: original game local order. "
            "No exported chunk overrides this IB, so local i maps to its original captured bone."
        )
    else:
        palette_comment = (
            f"; Stage 2 palette: exported chunk {local_palette_record.resource_suffix} "
            f"({local_palette_record.local_bone_count} local bones)"
        )
    return [
        f"[ResourceBoneMeta_{hash_value}]",
        "type = Buffer",
        "format = R32_FLOAT",
        f"data = {float(part_record.global_bone_base):.1f} {float(part_record.bone_count):.1f}",
        "",
        f"[TextureOverride_IB_{hash_value}_merge]",
        f"hash = {hash_value}",
        f"match_index_count = {part_record.match_index_count}",
        "match_priority = -500",
        "if vs == 200",
        "  run = CustomShader_ExtractCB1",
        f"  cs-t2 = ResourceBoneMeta_{hash_value}",
        "  run = CustomShader_RecordBones",
        "endif",
        palette_comment,
        "run = CustomShader_ExtractCB1",
        f"cs-t2 = ResourceLocalPalette_{local_palette_record.resource_suffix}",
        f"cs-t3 = ResourceLocalPaletteMeta_{local_palette_record.resource_suffix}",
        "run = CustomShader_GatherBones",
        "vs-t0 = ResourceLocalFakeT0_SRV",
        "run = CustomShader_RedirectCB1",
        "vs-cb1 = ResourceFakeCB1",
    ]


def build_bonestore_ini_content(
    part_records: list[PartRecord],
    local_palette_records: list[LocalPaletteRecord],
    namespace: str | None = None,
) -> str:
    unique_part_records = _deduplicate_part_records(part_records)
    effective_palette_records = _build_effective_palette_records(unique_part_records, local_palette_records)
    lines = []
    if namespace:
        lines.append(f"namespace = {namespace}")
        lines.append("")
    lines.append("; Auto-generated by SSMT Bone Palette Export Node")
    lines.append("; Stage 1: capture original per-IB palettes into the global bone buffer")
    lines.append("; Stage 2: gather the final exported chunk's required bones into a local small buffer")
    lines.append(";")
    lines.append("; IMPORTANT:")
    lines.append(";   ResourceBoneMeta_<ib>.y      = source/capture bone count for that original IB")
    lines.append(";   ResourceLocalPaletteMeta.x   = local palette bone count for one final exported chunk")
    lines.append(";   These are different concepts and should not be mixed.")
    lines.append("")
    lines.extend(_shader_override_sections(unique_part_records))
    lines.append("")
    lines.extend(_shared_resource_sections())
    lines.append("")
    lines.extend(_local_palette_sections(effective_palette_records))
    if effective_palette_records:
        lines.append("")
    palette_by_identity_key = {
        (record.ib_hash, record.match_index_count, record.chunk_index): record for record in effective_palette_records
    }
    for part_record in unique_part_records:
        local_palette_record = palette_by_identity_key[(part_record.ib_hash, part_record.match_index_count, 0)]
        lines.extend(_part_sections(part_record, local_palette_record))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _part_record_from_manifest_record(record: dict) -> PartRecord:
    return PartRecord(
        draw_index=int(record.get("draw_index", 0)),
        object_name=str(record.get("object_name", "")),
        vs_hash=str(record.get("vs_hash", "")).lower(),
        ib_hash=str(record.get("ib_hash", "")).lower(),
        match_index_count=int(record.get("match_index_count", 0)),
        bone_count=int(record.get("capture_bone_count", record.get("bone_count", 0))),
        global_bone_base=int(record.get("global_bone_base", 0)),
        vb2_path=str(record.get("vb2_path", "")),
        vs_t0_path=str(record.get("vs_t0_path", "")),
        vs_cb1_path=str(record.get("vs_cb1_path", "")),
        vs_cb1_first_constant=int(record.get("vs_cb1_first_constant", -1)),
        vs_cb1_num_constants=int(record.get("vs_cb1_num_constants", -1)),
    )


def _regenerate_bonestore_ini_if_possible(
    output_dir: str,
    local_palette_records: list[LocalPaletteRecord],
    logger: BonePaletteDebugLogger,
    capture_manifest_path: str | None = None,
) -> str:
    manifest_path = os.path.abspath(capture_manifest_path or "") if capture_manifest_path else ""
    if not manifest_path or not os.path.exists(manifest_path):
        manifest_path = os.path.join(output_dir, CAPTURE_MANIFEST_FILE_NAME)
    if not os.path.exists(manifest_path):
        logger.log(f"未找到 capture manifest，跳过 BoneStore.ini 重建: {manifest_path}")
        return os.path.join(output_dir, BONESTORE_INI_FILE_NAME)

    manifest = read_json(manifest_path)
    part_records = [_part_record_from_manifest_record(record) for record in manifest.get("part_records", [])]
    if not part_records:
        logger.log("capture manifest 中不包含 part_records，跳过 BoneStore.ini 重建")
        return os.path.join(output_dir, BONESTORE_INI_FILE_NAME)

    logger.log(f"根据 capture manifest 重建 BoneStore.ini，part_records={len(part_records)}")

    unique_part_records = _deduplicate_part_records(part_records)
    effective_records = _build_effective_palette_records(unique_part_records, local_palette_records)

    existing_keys = {
        (record.ib_hash.lower(), int(record.match_index_count), int(record.chunk_index))
        for record in local_palette_records
    }
    buffer_directory = os.path.join(output_dir, BUFFER_EXPORT_DIR_NAME)
    os.makedirs(buffer_directory, exist_ok=True)

    materialized_records = []
    for record in effective_records:
        file_name = f"{record.ib_hash.lower()}-{int(record.match_index_count)}-{int(record.chunk_index)}-Palette.buf"
        file_path = os.path.join(buffer_directory, file_name)
        key = (record.ib_hash.lower(), int(record.match_index_count), int(record.chunk_index))
        if key not in existing_keys:
            write_uint32_buffer(file_path, record.palette_values)
        materialized_records.append(
            LocalPaletteRecord(
                object_name=record.object_name,
                ib_hash=record.ib_hash.lower(),
                match_index_count=int(record.match_index_count),
                chunk_index=int(record.chunk_index),
                local_bone_count=int(record.local_bone_count),
                palette_values=tuple(int(value) for value in record.palette_values),
                file_name=file_name,
                file_path=file_path,
                resource_suffix=record.resource_suffix,
            )
        )

    os.makedirs(output_dir, exist_ok=True)
    ini_path = os.path.join(output_dir, BONESTORE_INI_FILE_NAME)
    namespace = build_bonestore_namespace(output_dir)
    with open(ini_path, "w", encoding="utf-8", newline="\n") as file_handle:
        file_handle.write(build_bonestore_ini_content(part_records, materialized_records, namespace=namespace))
    return ini_path


def _parse_chunk_identity(object_name: str) -> tuple[str, int, int] | None:
    match = _CHUNK_PREFIX_RE.match(str(object_name or "").strip())
    if not match:
        return None
    return match.group("hash").lower(), int(match.group("count")), int(match.group("chunk") or 0)


def _parse_numeric_group(group_name: str) -> int | None:
    raw_name = str(group_name).strip()
    if not _NUMERIC_GROUP_RE.match(raw_name):
        return None
    return int(raw_name)


def _get_existing_localized_palette(mesh_obj) -> tuple[int, ...] | None:
    raw_palette = mesh_obj.get(EXPORT_PALETTE_PROP)
    if not raw_palette:
        return None
    try:
        return tuple(int(value) for value in raw_palette)
    except (TypeError, ValueError):
        return None


def _assert_mesh_is_global_source(mesh_obj) -> None:
    localized_palette = _get_existing_localized_palette(mesh_obj)
    state = str(mesh_obj.get(VERTEX_GROUP_STATE_PROP, "") or "")
    export_chunk = str(mesh_obj.get(EXPORT_CHUNK_PROP, "") or "")
    if localized_palette is None and state != VERTEX_GROUP_STATE_EXPORT_LOCAL and not export_chunk:
        return
    raise ValueError(
        f"{mesh_obj.name}: 输入到 Bone Palette 节点的对象必须是干净的全局网格，不能是旧的导出副本"
    )


def _build_group_index_to_global_map(mesh_obj) -> dict[int, int]:
    group_index_to_global = {}
    for vertex_group in mesh_obj.vertex_groups:
        numeric_group = _parse_numeric_group(vertex_group.name)
        if numeric_group is None:
            continue
        group_index_to_global[vertex_group.index] = numeric_group
    return group_index_to_global


def _iter_weighted_global_assignments(mesh_obj):
    group_index_to_global = _build_group_index_to_global_map(mesh_obj)
    for vertex in mesh_obj.data.vertices:
        for group_element in vertex.groups:
            global_group = group_index_to_global.get(int(group_element.group))
            if global_group is None:
                continue
            weight = float(group_element.weight)
            if weight <= 0.0:
                continue
            yield global_group, vertex.index, weight


def _collect_used_numeric_vertex_groups(mesh_obj) -> set[int]:
    used_groups = {global_group for global_group, _vertex_index, _weight in _iter_weighted_global_assignments(mesh_obj)}
    if not used_groups:
        raise ValueError(f"{mesh_obj.name}: 未找到任何带权重的纯数字顶点组")
    return used_groups


def _inspect_mesh_prepare_state(mesh_obj) -> MeshPrepareState:
    localized_palette = _get_existing_localized_palette(mesh_obj)
    state = str(mesh_obj.get(VERTEX_GROUP_STATE_PROP, "") or "")
    export_chunk = str(mesh_obj.get(EXPORT_CHUNK_PROP, "") or "")

    if localized_palette is None and state != VERTEX_GROUP_STATE_EXPORT_LOCAL and not export_chunk:
        used_groups = tuple(sorted(_collect_used_numeric_vertex_groups(mesh_obj)))
        return MeshPrepareState(
            mesh_obj=mesh_obj,
            is_localized=False,
            localized_palette=(),
            export_chunk="",
            used_global_groups=used_groups,
        )

    if localized_palette is None:
        raise ValueError(f"{mesh_obj.name}: 已带有导出本地化痕迹，但元数据不完整")

    used_local_groups = _collect_used_numeric_vertex_groups(mesh_obj)
    if any(local_index < 0 or local_index >= len(localized_palette) for local_index in used_local_groups):
        raise ValueError(f"{mesh_obj.name}: 当前本地顶点组索引超出保存的 palette 范围")

    used_global_groups = tuple(sorted({int(localized_palette[local_index]) for local_index in used_local_groups}))
    return MeshPrepareState(
        mesh_obj=mesh_obj,
        is_localized=True,
        localized_palette=localized_palette,
        export_chunk=export_chunk,
        used_global_groups=used_global_groups,
    )


def _validate_localized_mesh(mesh_obj, palette: tuple[int, ...]) -> dict:
    if len(mesh_obj.vertex_groups) != len(palette):
        raise ValueError(
            f"{mesh_obj.name}: 本地化后的顶点组数量 {len(mesh_obj.vertex_groups)} 与 palette 数量 {len(palette)} 不一致"
        )

    invalid_slots = []
    for expected_local_index in range(len(palette)):
        try:
            vertex_group = mesh_obj.vertex_groups[expected_local_index]
        except Exception as error:
            raise ValueError(
                f"{mesh_obj.name}: 缺少本地顶点组槽位 {expected_local_index}"
            ) from error

        if vertex_group.index != expected_local_index or vertex_group.name != str(expected_local_index):
            invalid_slots.append(
                {
                    "expected": expected_local_index,
                    "actual_index": int(vertex_group.index),
                    "actual_name": str(vertex_group.name),
                }
            )

    if invalid_slots:
        preview = ", ".join(
            f"slot {item['expected']} -> index={item['actual_index']}, name={item['actual_name']}"
            for item in invalid_slots[:8]
        )
        raise ValueError(f"{mesh_obj.name}: 本地化后顶点组未连续从 0 开始: {preview}")

    used_local_groups = tuple(sorted(_collect_used_numeric_vertex_groups(mesh_obj)))
    expected_global_groups = tuple(sorted(int(value) for value in palette))
    if tuple(int(value) for value in mesh_obj.get(EXPORT_PALETTE_PROP, [])) != expected_global_groups:
        raise ValueError(f"{mesh_obj.name}: 本地化后保存的 palette 元数据与目标 palette 不一致")

    return {
        "group_count": len(mesh_obj.vertex_groups),
        "used_local_groups": used_local_groups,
        "used_local_preview": _format_group_preview(used_local_groups),
        "palette_preview": _format_group_preview(expected_global_groups),
    }


def localize_vertex_groups_for_palette(mesh_obj, palette: tuple[int, ...], chunk_name: str = "") -> None:
    global_to_local = {global_group: local_index for local_index, global_group in enumerate(palette)}
    weights_by_local = {local_index: {} for local_index in range(len(palette))}
    for global_group, vertex_index, weight in _iter_weighted_global_assignments(mesh_obj):
        local_index = global_to_local.get(global_group)
        if local_index is None:
            continue
        previous_weight = weights_by_local[local_index].get(vertex_index, 0.0)
        weights_by_local[local_index][vertex_index] = max(previous_weight, weight)

    for vertex_group in list(mesh_obj.vertex_groups):
        mesh_obj.vertex_groups.remove(vertex_group)

    for local_index in range(len(palette)):
        vertex_group = mesh_obj.vertex_groups.new(name=str(local_index))
        assignments = weights_by_local.get(local_index, {})
        for vertex_index, weight in assignments.items():
            vertex_group.add([vertex_index], weight, 'REPLACE')

    mesh_obj[EXPORT_PALETTE_PROP] = list(palette)
    mesh_obj[VERTEX_GROUP_STATE_PROP] = VERTEX_GROUP_STATE_EXPORT_LOCAL
    if chunk_name:
        mesh_obj[EXPORT_CHUNK_PROP] = chunk_name


def _remove_object_from_blender(obj) -> None:
    object_type = obj.type
    data_block = getattr(obj, "data", None)
    bpy.data.objects.remove(obj, do_unlink=True)
    if object_type == 'MESH' and data_block is not None and data_block.users == 0:
        bpy.data.meshes.remove(data_block)


def _generate_unique_name(base_name: str) -> str:
    if bpy.data.objects.get(base_name) is None:
        return base_name
    suffix = 1
    while bpy.data.objects.get(f"{base_name}.{suffix:03d}") is not None:
        suffix += 1
    return f"{base_name}.{suffix:03d}"


def _build_stage_clone_name(source_name: str, stage_suffix: str) -> str:
    clean_name = str(source_name or "")
    if clean_name.endswith("_copy"):
        return f"{clean_name[:-5]}{stage_suffix}_copy"
    return f"{clean_name}{stage_suffix}"


def _build_export_clone_name(source_name: str) -> str:
    return _build_stage_clone_name(source_name, "_BPE")


def _format_group_preview(values, limit: int = 12) -> str:
    ordered_values = [int(value) for value in values]
    if not ordered_values:
        return "[]"
    if len(ordered_values) <= limit:
        return f"{ordered_values}"
    preview = ", ".join(str(value) for value in ordered_values[:limit])
    return f"[{preview}, ...]"


def _format_global_to_local_preview(source_groups, palette: tuple[int, ...], limit: int = 10) -> str:
    palette_index = {int(global_group): local_index for local_index, global_group in enumerate(palette)}
    ordered_source_groups = [int(value) for value in source_groups]
    pairs = []
    for global_group in ordered_source_groups[:limit]:
        local_index = palette_index.get(global_group)
        if local_index is None:
            pairs.append(f"{global_group}->missing")
        else:
            pairs.append(f"{global_group}->{local_index}")
    if len(ordered_source_groups) > limit:
        pairs.append("...")
    return ", ".join(pairs)


def _resolve_capture_manifest_path(context, output_dir: str) -> str:
    scene = getattr(context, "scene", None)
    if scene is not None:
        raw_manifest_path = str(getattr(scene, "bmc_manifest_path", "") or "")
        if raw_manifest_path:
            manifest_path = bpy.path.abspath(raw_manifest_path)
            manifest_path = os.path.abspath(manifest_path)
            if os.path.exists(manifest_path):
                return manifest_path
    return os.path.join(output_dir, CAPTURE_MANIFEST_FILE_NAME)


def _validate_single_chunk_membership_from_entries(chain_entries: list[dict]) -> None:
    memberships = {}
    for entry in chain_entries:
        source_name = str(entry["source_obj"].name)
        identity = tuple(entry["identity"])
        memberships.setdefault(source_name, set()).add(identity)

    duplicated = {
        source_name: identities
        for source_name, identities in memberships.items()
        if len(identities) > 1
    }
    if not duplicated:
        return

    source_name, identities = next(iter(duplicated.items()))
    identity_names = [f"{item[0]}-{item[1]}-{item[2]}" for item in sorted(identities)]
    raise ValueError(
        f"{source_name}: 同一个对象同时出现在多个导出 Chunk 中: {', '.join(identity_names)}，"
        "这和 bmc_prepare_export_collection 的单 Chunk 归属要求不一致"
    )


def _cleanup_previous_generated_objects(node_name: str, logger: BonePaletteDebugLogger) -> None:
    removed_count = 0
    for obj in list(bpy.data.objects):
        if str(obj.get(GENERATED_NODE_PROP, "") or "") != node_name:
            continue
        _remove_object_from_blender(obj)
        removed_count += 1
    if removed_count:
        logger.log(f"清理旧导出副本: {removed_count} 个")


def _clone_generated_object(source_obj, node_name: str, *, stage: str, name_builder, source_name: str | None = None):
    build_obj = source_obj.copy()
    if source_obj.data is not None:
        build_obj.data = source_obj.data.copy()
    build_obj.name = _generate_unique_name(name_builder(source_obj.name))
    build_obj[GENERATED_NODE_PROP] = node_name
    build_obj[GENERATED_SOURCE_PROP] = str(source_name or source_obj.name)
    build_obj[GENERATED_STAGE_PROP] = stage
    for prop_name in (EXPORT_PALETTE_PROP, EXPORT_CHUNK_PROP):
        if prop_name in build_obj:
            del build_obj[prop_name]
    build_obj[VERTEX_GROUP_STATE_PROP] = VERTEX_GROUP_STATE_GLOBAL
    return build_obj


class SSMTNode_BonePalette_Export(SSMTNodeBase):
    bl_idname = 'SSMTNode_BonePalette_Export'
    bl_label = 'Bone Palette 导出'
    bl_icon = 'ARMATURE_DATA'
    bl_width_min = 340

    output_dir: bpy.props.StringProperty(name="输出目录", subtype='DIR_PATH', default="")  # type: ignore
    last_manifest_path: bpy.props.StringProperty(name="最近 Manifest", subtype='FILE_PATH', default="")  # type: ignore
    last_bonestore_ini_path: bpy.props.StringProperty(name="最近 BoneStore", subtype='FILE_PATH', default="")  # type: ignore
    last_debug_text_name: bpy.props.StringProperty(name="调试文本", default="")  # type: ignore
    last_error_message: bpy.props.StringProperty(name="最后错误", default="")  # type: ignore

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 340

    def draw_buttons(self, context, layout):
        box = layout.box()
        box.label(text="Bone Palette 导出", icon='ARMATURE_DATA')
        box.prop(self, "output_dir", text="输出目录")
        box.label(text="Generate Mod 时自动执行", icon='INFO')
        box.label(text="输入物体会按名称前缀自动分组")
        if self.last_manifest_path:
            box.separator()
            box.label(text=f"Manifest: {os.path.basename(self.last_manifest_path)}")
        if self.last_bonestore_ini_path:
            box.label(text=f"BoneStore: {os.path.basename(self.last_bonestore_ini_path)}")
        if self.last_debug_text_name:
            box.label(text=f"调试文本: {self.last_debug_text_name}")
        if self.last_error_message:
            error_box = box.box()
            error_box.alert = True
            error_box.label(text="最近错误", icon='ERROR')
            error_box.label(text=self.last_error_message[:120])

    def resolve_output_dir(self) -> str:
        raw_output_dir = self.output_dir or ""
        if not raw_output_dir:
            scene = getattr(bpy.context, "scene", None)
            if scene is not None:
                raw_output_dir = str(
                    getattr(scene, "bmc_output_dir", "")
                    or getattr(scene, "bmc_frameanalysis_dir", "")
                    or ""
                )
        normalized_path = bpy.path.abspath(raw_output_dir)
        normalized_path = os.path.abspath(normalized_path) if normalized_path else ""
        if not normalized_path:
            raise ValueError(f"节点 '{self.name}' 未设置输出目录，且场景中也没有可回退的 bmc_output_dir/bmc_frameanalysis_dir")
        return normalized_path

    @staticmethod
    def generate_debug_summary(processing_chains: list) -> str:
        affected_chains = 0
        affected_nodes = 0
        for chain in processing_chains:
            node_names = [node for node in chain.node_path if node.bl_idname == 'SSMTNode_BonePalette_Export']
            if node_names:
                affected_chains += 1
                affected_nodes += len(node_names)
        return f"Bone Palette 导出: {affected_nodes} 个节点 (影响 {affected_chains} 个物体)"

    @staticmethod
    def generate_debug_detail(chain) -> list:
        node_names = [node.name for node in chain.node_path if node.bl_idname == 'SSMTNode_BonePalette_Export']
        if not node_names:
            return []
        details = [f"Bone Palette 节点: {', '.join(node_names)}"]
        if getattr(chain, 'export_object_name_override', ''):
            details.append(f"导出对象覆盖: {chain.export_object_name_override}")
        return details

    @staticmethod
    def execute_batch_from_chains(valid_chains: list) -> dict:
        ordered_nodes = []
        seen_node_keys = set()
        for chain in valid_chains:
            for node in chain.node_path:
                if node.bl_idname != 'SSMTNode_BonePalette_Export':
                    continue
                tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
                node_key = f"{tree_name}::{node.name}"
                if node_key in seen_node_keys:
                    continue
                ordered_nodes.append(node)
                seen_node_keys.add(node_key)

        if not ordered_nodes:
            return {"processed_count": 0, "node_count": 0}

        processed_count = 0
        for node in ordered_nodes:
            processed_count += node._process_chains_for_node(valid_chains)
        return {"processed_count": processed_count, "node_count": len(ordered_nodes)}

    def _process_chains_for_node(self, valid_chains: list) -> int:
        logger = BonePaletteDebugLogger(self.name)
        self.last_error_message = ""

        try:
            context = bpy.context
            output_dir = ensure_directory(self.resolve_output_dir())
            buffer_dir = ensure_directory(os.path.join(output_dir, BUFFER_EXPORT_DIR_NAME))
            hlsl_dir = export_required_hlsl(output_dir)
            capture_manifest_path = _resolve_capture_manifest_path(context, output_dir)

            logger.log(f"节点开始处理: {self.name}")
            logger.log(f"输出目录: {output_dir}")
            logger.log(f"Buffer 目录: {buffer_dir}")
            logger.log(f"HLSL 目录: {hlsl_dir}")
            logger.log(f"capture manifest: {capture_manifest_path}")

            _cleanup_previous_generated_objects(self.name, logger)

            chain_entries = []
            invalid_name_objects = []

            for chain in valid_chains:
                if self not in chain.node_path:
                    continue

                obj = bpy.data.objects.get(chain.object_name)
                if not obj and chain.original_object_name:
                    obj = bpy.data.objects.get(chain.original_object_name)
                if obj is None:
                    raise ValueError(f"链路对象不存在: {chain.object_name}")
                if obj.type != 'MESH':
                    raise ValueError(f"Bone Palette 节点仅支持网格对象: {obj.name}")

                identity = _parse_chunk_identity(obj.name)
                if identity is None:
                    invalid_name_objects.append(obj.name)
                    continue

                _assert_mesh_is_global_source(obj)
                chain_entries.append({
                    "chain": chain,
                    "source_obj": obj,
                    "identity": identity,
                })
                logger.log(
                    f"收集链路对象: chain={chain.object_name}, source={obj.name}, identity={identity[0]}-{identity[1]}-{identity[2]}"
                )

            if invalid_name_objects:
                preview = ", ".join(invalid_name_objects[:10])
                raise ValueError(
                    "Bone Palette 节点收到无法解析前缀的物体，要求名称以 '<ib_hash>-<match_index_count>[-<chunk_index>]' 开头: "
                    f"{preview}"
                )

            _validate_single_chunk_membership_from_entries(chain_entries)

            if not chain_entries:
                logger.log("没有任何链路对象经过当前 Bone Palette 节点")
                self.last_manifest_path = ""
                self.last_bonestore_ini_path = os.path.join(output_dir, BONESTORE_INI_FILE_NAME)
                return 0

            grouped_entries = {}
            for entry in chain_entries:
                grouped_entries.setdefault(entry["identity"], []).append(entry)
            logger.log(f"按物体名前缀分组完成: {len(grouped_entries)} 个 Chunk, {len(chain_entries)} 个对象")

            palette_records = []
            local_palette_records = []
            object_records = []

            for identity in sorted(grouped_entries.keys()):
                ib_hash, match_index_count, chunk_index = identity
                chunk_name = f"{ib_hash}-{match_index_count}-{chunk_index}"
                grouped_source_entries = {}
                for entry in grouped_entries[identity]:
                    grouped_source_entries.setdefault(entry["source_obj"].name, []).append(entry)

                logger.log(
                    f"处理 Chunk: {chunk_name}, 链路数 {len(grouped_entries[identity])}, 唯一源对象数 {len(grouped_source_entries)}"
                )

                mesh_states = []
                for source_name in sorted(grouped_source_entries.keys()):
                    source_entries = grouped_source_entries[source_name]
                    source_entry = source_entries[0]
                    export_obj = _clone_generated_object(
                        source_entry["source_obj"],
                        self.name,
                        stage=GENERATED_STAGE_PROCESSED,
                        name_builder=_build_export_clone_name,
                        source_name=source_entry["source_obj"].name,
                    )
                    mesh_state = _inspect_mesh_prepare_state(export_obj)
                    mesh_states.append((source_entries, mesh_state, export_obj))
                    logger.log(
                        f"  克隆对象: {source_entry['source_obj'].name} -> 处理副本 {export_obj.name}, 关联链路 {len(source_entries)}"
                    )

                global_groups = set()
                for _source_entries, mesh_state, _export_obj in mesh_states:
                    global_groups.update(mesh_state.used_global_groups)
                if not global_groups:
                    raise ValueError(f"{chunk_name}: 未找到任何带权重的纯数字顶点组")

                palette = tuple(sorted(global_groups))
                logger.log(f"  Chunk Palette 骨骼数: {len(palette)}")
                logger.log(f"  Chunk Palette 预览: {_format_group_preview(palette)}")
                if len(palette) > BI4_MAX_BONE_COUNT:
                    raise ValueError(
                        f"{chunk_name}: 顶点组 palette 数量 {len(palette)} 超出 {BI4_MAX_BONE_COUNT} 上限"
                    )

                file_name = f"{ib_hash}-{match_index_count}-{chunk_index}-Palette.buf"
                file_path = os.path.join(buffer_dir, file_name)
                resource_suffix = f"{ib_hash}_{match_index_count}_{chunk_index}"
                palette_records.append(
                    {
                        "ib_hash": ib_hash,
                        "match_index_count": match_index_count,
                        "chunk_index": chunk_index,
                        "chunk_collection": chunk_name,
                        "local_bone_count": len(palette),
                        "file_name": file_name,
                        "file_path": file_path,
                        "resource_suffix": resource_suffix,
                        "palette_values": list(palette),
                    }
                )
                local_palette_records.append(
                    LocalPaletteRecord(
                        object_name=chunk_name,
                        ib_hash=ib_hash,
                        match_index_count=match_index_count,
                        chunk_index=chunk_index,
                        local_bone_count=len(palette),
                        palette_values=palette,
                        file_name=file_name,
                        file_path=file_path,
                        resource_suffix=resource_suffix,
                    )
                )

                for source_entries, mesh_state, export_obj in mesh_states:
                    localize_vertex_groups_for_palette(export_obj, palette, chunk_name)
                    localized_state = _validate_localized_mesh(export_obj, palette)
                    global_to_local_preview = _format_global_to_local_preview(mesh_state.used_global_groups, palette)

                    for entry in source_entries:
                        entry["chain"].object_name = export_obj.name
                        entry["chain"].export_object_name_override = export_obj.name

                    primary_entry = source_entries[0]
                    object_records.append(
                        {
                            "object": export_obj.name,
                            "source_object": primary_entry["source_obj"].name,
                            "chunk_collection": chunk_name,
                            "host_ib_hash": ib_hash,
                            "host_match_index_count": match_index_count,
                            "chunk_index": chunk_index,
                            "palette_file": file_name,
                            "local_bone_count": len(palette),
                            "source_used_global_groups": list(mesh_state.used_global_groups),
                            "used_local_groups": list(localized_state["used_local_groups"]),
                            "global_to_local_preview": global_to_local_preview,
                            "linked_chain_count": len(source_entries),
                        }
                    )
                    logger.log(
                        f"  本地化完成: {export_obj.name}, 源数字顶点组={len(mesh_state.used_global_groups)}, "
                        f"source_preview={_format_group_preview(mesh_state.used_global_groups)}, "
                        f"本地组槽位={localized_state['group_count']}, used_local={localized_state['used_local_preview']}, "
                        f"palette={localized_state['palette_preview']}, 映射样例={global_to_local_preview}"
                    )

            for lpr in local_palette_records:
                write_uint32_buffer(lpr.file_path, lpr.palette_values)

            manifest = {
                "node_name": self.name,
                "grouping_mode": "blueprint_object_prefix",
                "output_dir": output_dir,
                "buffer_dir": buffer_dir,
                "bonestore_namespace": build_bonestore_namespace(output_dir),
                "palettes": palette_records,
                "objects": object_records,
                "note": "Blueprint input objects are treated as export sources, duplicated into processed build copies that are localized before writing runtime files.",
            }
            manifest_path = write_json(os.path.join(output_dir, EXPORT_MANIFEST_FILE_NAME), manifest)
            bonestore_ini_path = _regenerate_bonestore_ini_if_possible(
                output_dir,
                local_palette_records,
                logger,
                capture_manifest_path=capture_manifest_path,
            )

            self.last_manifest_path = manifest_path
            self.last_bonestore_ini_path = bonestore_ini_path
            scene = getattr(context, "scene", None)
            if scene is not None and hasattr(scene, "bmc_export_manifest_path"):
                scene.bmc_export_manifest_path = manifest_path
            if scene is not None and hasattr(scene, "bmc_ini_path") and bonestore_ini_path:
                scene.bmc_ini_path = bonestore_ini_path
            logger.log(f"写出 export_manifest.json: {manifest_path}")
            logger.log(f"BoneStore.ini 路径: {bonestore_ini_path}")
            logger.log(f"节点处理完成: {len(chain_entries)} 个对象")
            return len(chain_entries)
        except Exception as error:
            self.last_error_message = str(error)
            logger.log_exception(error)
            raise
        finally:
            self.last_debug_text_name = logger.flush()


classes = (
    SSMTNode_BonePalette_Export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)