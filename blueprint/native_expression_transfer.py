"""Transfer evaluated ZZMI facial vertices in the exported vertex order."""

import json
from pathlib import Path
from typing import NamedTuple

import numpy as np


SHADER_NAME = "zzmi_native_expression_transfer.hlsl"
POSITION_STRIDE = 40
THREADS_PER_GROUP = 64


class TransferMesh(NamedTuple):
    position_override: str
    position_resource: str
    position_file: str
    blend_override: str
    blend_resource: str
    exported_basis: np.ndarray
    source_vertices: np.ndarray
    native_basis: np.ndarray


def match_native_vertex_order(exported, parts, texcoords=None):
    """Return a complete, unambiguous output-to-native vertex mapping."""
    if not parts or not len(exported):
        return None
    native = parts[0][0]
    indices = np.full(len(exported), -1, dtype=np.int64)
    for basis, source_ib, export_ib in parts:
        if (
            not np.array_equal(basis, native)
            or len(source_ib) != len(export_ib)
            or not len(source_ib)
            or source_ib.max() >= len(basis)
            or export_ib.max() >= len(exported)
        ):
            return None
        previous = indices[export_ib]
        if np.any((previous >= 0) & (previous != source_ib)):
            return None
        indices[export_ib] = source_ib
        if np.any(indices[export_ib] != source_ib):
            return None
    if np.any(indices < 0) or np.any(indices >= len(native)):
        return None
    if not np.array_equal(exported, native[indices]):
        if texcoords is None:
            return None
        native_texcoords, exported_texcoords = texcoords
        if (
            native_texcoords.shape[1:] != exported_texcoords.shape[1:]
            or len(native_texcoords) != len(native)
            or len(exported_texcoords) != len(exported)
            or not np.allclose(native_texcoords[indices], exported_texcoords, rtol=0, atol=1e-7)
        ):
            return None
    return indices.astype("<u4")


def collect_builder_sections(blocks):
    sections = {}
    owners = {}
    for block in blocks:
        header = f"[{block.SectionName}]" if block.SectionName else ""
        if header:
            sections[header] = []
            owners[header] = (block, False)
        for line in block.SectionLineList:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                header = stripped
                sections[header] = []
                owners[header] = (block, True)
            elif header:
                sections[header].append(line)
    return sections, owners


def _value(sections, section, key):
    for line in sections.get(section, ()):
        lhs, _, rhs = line.partition("=")
        if lhs.strip() == key:
            return rhs.strip()
    return ""


def _read_texcoords(source, identity, exported_path, native_vertex_count, exported_vertex_count):
    # Blender's UV round trip can differ in the last float bits. Only use this
    # tolerant identity check for workspaces with a verified float32 layout.
    with (source / f"{identity}.json").open(encoding="utf-8-sig") as file:
        metadata = json.load(file)
    native_path = f"{identity}-Texcoord.buf"
    supported_formats = {"R32_FLOAT", "R32G32_FLOAT", "R32G32B32_FLOAT", "R32G32B32A32_FLOAT"}
    for entry in metadata.get("CategoryBufferList", ()):
        if entry.get("FileName") != native_path:
            continue
        elements = entry.get("D3D11ElementList", ())
        if elements and all(element.get("Format") in supported_formats for element in elements):
            return (
                np.fromfile(source / native_path, dtype="<f4").reshape(native_vertex_count, -1),
                np.fromfile(exported_path, dtype="<f4").reshape(exported_vertex_count, -1),
            )
    return None


def read_transfer_mesh(sections, output, draw_hash, workspace_folder, game_type_folder):
    output = Path(output)
    position_overrides = [
        (section, _value(sections, section, "vb0"))
        for section in sections
        if section.startswith(f"[TextureOverride_VB_{draw_hash}_")
        and section.endswith("_Position]")
    ]
    if len(position_overrides) != 1:
        return None
    position_override, position_resource = position_overrides[0]
    if not position_resource or not _value(sections, position_override, "hash"):
        return None
    blend_overrides = [
        section
        for section in sections
        if section.startswith(f"[TextureOverride_VB_{draw_hash}_")
        and section.endswith("_Blend]")
    ]
    if len(blend_overrides) != 1 or not _value(sections, blend_overrides[0], "hash"):
        return None
    blend_override = blend_overrides[0]
    blend_resource = (
        _value(sections, blend_override, "vb2")
        or _value(sections, position_override, "vb2")
    )
    if not blend_resource:
        return None
    resource = f"[{position_resource}]"
    filename = _value(sections, resource, "filename")
    if not filename or _value(sections, resource, "stride") != str(POSITION_STRIDE):
        return None
    exported = np.fromfile(output / filename, dtype="<f4").reshape(-1, 10)
    parts = []
    first_source = None
    for section in sections:
        if (
            not section.startswith("[TextureOverride_LOD")
            or _value(sections, section, "hash") != draw_hash
        ):
            continue
        ib_resource = _value(sections, section, "ib")
        ib_path = _value(sections, f"[{ib_resource}]", "filename")
        ib_format = _value(sections, f"[{ib_resource}]", "format")
        index_format = {"DXGI_FORMAT_R32_UINT": "<u4", "DXGI_FORMAT_R16_UINT": "<u2"}.get(ib_format)
        if not ib_path or index_format is None:
            return None
        part = Path(ib_path).stem.removesuffix("-Index")
        _, separator, identity = part.partition(".")
        if not separator or not identity.startswith(draw_hash + "-"):
            return None
        source = Path(workspace_folder(part)) / game_type_folder
        basis = np.fromfile(source / f"{identity}-Position.buf", dtype="<f4").reshape(-1, 10)
        source_ib = np.fromfile(source / f"{identity}.ib", dtype="<u4")
        export_ib = np.fromfile(output / ib_path, dtype=index_format)
        if first_source is None:
            first_source = source, identity
        parts.append((basis, source_ib, export_ib))
    mapping = match_native_vertex_order(exported, parts)
    if mapping is None and parts:
        texcoord_resource = position_resource.removesuffix("Position") + "Texcoord"
        texcoord_file = _value(sections, f"[{texcoord_resource}]", "filename")
        if texcoord_file:
            texcoords = _read_texcoords(
                *first_source,
                output / texcoord_file,
                len(parts[0][0]),
                len(exported),
            )
            mapping = match_native_vertex_order(exported, parts, texcoords)
    if mapping is None:
        return None
    return TransferMesh(
        position_override,
        position_resource,
        filename,
        blend_override,
        blend_resource,
        exported,
        mapping,
        parts[0][0],
    )


def matches_imported_shape_keys(exported, mapping, expected_by_name, hash_slots, overrides):
    names = {name for slot in hash_slots.values() for name in slot}
    if names != expected_by_name.keys():
        return False
    for slot, shapes in hash_slots.items():
        for name, objects in shapes.items():
            source_delta = expected_by_name[name]
            dense = np.zeros((len(exported), 3), dtype="<f4")
            np.add.at(dense, source_delta.vertex_ids, source_delta.deltas[:, :3])
            covered = np.zeros(len(exported), dtype=bool)
            for obj in objects:
                record = overrides.get(slot, {}).get(obj, {})
                rows = np.asarray(record.get("export_indices", ()), dtype=np.int64)
                sampled_bytes = record.get("position_bytes", b"")
                if not rows.size or len(sampled_bytes) != rows.size * POSITION_STRIDE:
                    return False
                expected = exported[rows, :3] + dense[mapping[rows]]
                sampled = np.frombuffer(sampled_bytes, dtype="<f4").reshape(-1, 10)[:, :3]
                if not np.allclose(sampled, expected, rtol=0, atol=1.6e-5):
                    return False
                covered[rows] = True
            if np.any(~covered & np.any(dense[mapping] != 0, axis=1)):
                return False
    return True


def _native_basis_filename(draw_hash, mapping_file):
    return Path(mapping_file).with_name(f"{draw_hash}-NativeExpressionBasis.buf").as_posix()


def write_transfer_buffers(output, draw_hash, mesh, mapping_file):
    from ..common.safe_write import write_bytes_if_changed_atomic

    output = Path(output)
    write_bytes_if_changed_atomic(output / mapping_file, mesh.source_vertices.tobytes())
    write_bytes_if_changed_atomic(output / _native_basis_filename(draw_hash, mapping_file), mesh.native_basis.tobytes())


def render_transfer_sections(draw_hash, mesh, mapping_file):
    stem = f"ZZMINativeExpression_{draw_hash}"
    current, source_map, static_position, native_basis, adjusted = (
        f"Resource{stem}_{part}"
        for part in ("Current", "Map", "StaticPosition", "NativeBasis", "AdjustedPosition")
    )
    shader = f"CustomShader{stem}"
    sections = {}
    sections[f"[{current}]"] = ["type = StructuredBuffer", f"stride = {POSITION_STRIDE}", "bind_flags = shader_resource"]
    sections[f"[{source_map}]"] = ["type = StructuredBuffer", "stride = 4", f"filename = {mapping_file}"]
    sections[f"[{static_position}]"] = [
        "type = StructuredBuffer", f"stride = {POSITION_STRIDE}",
        f"filename = {mesh.position_file}",
    ]
    sections[f"[{native_basis}]"] = [
        "type = StructuredBuffer", f"stride = {POSITION_STRIDE}",
        f"filename = {_native_basis_filename(draw_hash, mapping_file)}",
    ]
    sections[f"[{adjusted}]"] = [
        "type = Buffer", f"stride = {POSITION_STRIDE}",
        f"array = {len(mesh.source_vertices)}",
        "bind_flags = unordered_access vertex_buffer",
    ]
    sections[f"[{shader}]"] = [
        f"cs = ./res/{SHADER_NAME}",
        f"{current} = copy vb0",
        f"cs-t105 = {current}",
        f"cs-t106 = {native_basis}",
        f"cs-t107 = {source_map}",
        f"cs-t108 = {static_position}",
        f"cs-u5 = copy {adjusted}",
        f"Dispatch = {(len(mesh.source_vertices) + THREADS_PER_GROUP - 1) // THREADS_PER_GROUP}, 1, 1",
        f"{adjusted} = copy cs-u5",
        f"vb0 = {adjusted}",
        "cs-t105 = null",
        "cs-t106 = null",
        "cs-t107 = null",
        "cs-t108 = null",
        "cs-u5 = null",
    ]

    return f"run = {shader}", sections


def install_transfer_sections(sections, draw_hash, mesh, mapping_file):
    run_command, resources = render_transfer_sections(draw_hash, mesh, mapping_file)
    vertex_count = len(mesh.source_vertices)
    native_vertex_count = len(mesh.native_basis)
    # Blend stays bound for final indexed draws; VertexCount isolates the preskin stream.
    match_vertex_count = f"match_vertex_count = {native_vertex_count}"
    generated_lines = {
        run_command,
        f"vb2 = {mesh.blend_resource}",
        f"vb0 = {mesh.position_resource}",
        match_vertex_count,
        "handling = skip",
        f"draw = {vertex_count}, 0",
    }
    position_lines = sections[mesh.position_override]
    position_lines[:] = [line for line in position_lines if line.strip() not in generated_lines]

    blend_lines = sections[mesh.blend_override]
    blend_lines[:] = [
        line for line in blend_lines
        if line.strip() not in {
            run_command,
            f"vb2 = {mesh.blend_resource}",
            match_vertex_count,
            "handling = skip",
            f"draw = {vertex_count}, 0",
        }
    ]
    insert_at = next(
        (index + 1 for index, line in enumerate(blend_lines) if line.strip().startswith("hash = ")),
        len(blend_lines),
    )
    blend_lines[insert_at:insert_at] = [
        match_vertex_count,
        run_command,
        f"vb2 = {mesh.blend_resource}",
        "handling = skip",
        f"draw = {vertex_count}, 0",
    ]
    sections.update(resources)
    return resources


def replace_builder_section(block, header, has_header, section_lines):
    """Replace one builder section without disturbing its neighboring sections."""
    lines = block.SectionLineList
    if has_header:
        start = lines.index(header) + 1
    else:
        start = 0
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].strip().startswith("[")
        ),
        len(lines),
    )
    lines[start:end] = list(section_lines)
