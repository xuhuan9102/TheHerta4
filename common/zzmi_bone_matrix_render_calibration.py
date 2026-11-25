"""Build a private skinning pass scoped to an exact ZZMI render override."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .zzmi_draw_calls import indexed_draw_ranges
from .zzmi_bone_matrix_layers import LAYER_CONTROL, LAYER_FALLBACK, LAYER_FULL, LAYER_UPDATE
from .zzmi_bone_matrix_render_inputs import (
    BLEND_LAYOUT_MARKER, BLEND_STRIDE, POSITION_STRIDE,
    ground_support_vertices, normalized_blend, remap_blend_indices, weighted_slots,
)
from .zzmi_bone_matrix_snapshot import ORIGINAL_CB1, SNAPSHOT_FRAME, SnapshotSections
from .zzmi_bone_physics import (
    AIR_DRAG, AIR_PARAMS_TAG, JOINT_OPTIONS, JOINT_PARAMS_TAG, BODIES, FOLLOWERS, RESET,
    PREVIEW_SHADER, PREVIEW_PIXEL_SHADER,
    SHADER as PHYSICS_SHADER,
)

RENDER_SHADER = "zzmi_bone_matrix_render_calibration.hlsl"
_TAG = "SSMT bone matrix"
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$", re.IGNORECASE)
_CAPTURE = re.compile(
    r"^([ \t]*)ResourceZZPalette_([0-9a-f]{8})_s(\d+)(?:_(LOD\d+))?\s*=\s*copy vs-t0\b",
    re.IGNORECASE,
)
_PALETTE = re.compile(r"^\s*vs-t0\s*=\s*(?:ref\s+)?(ResourceZZ\w+_s(\d+)(?:_LOD\d+)?)\s*$", re.IGNORECASE)
_OCCURRENCE = re.compile(r"\$([a-z0-9_]*?)zz_ms_occ_(\d+)\b", re.IGNORECASE)
_IB = re.compile(r"^\s*hash\s*=\s*([0-9a-f]{8})\s*$", re.IGNORECASE)
_LOD_SUFFIX = re.compile(r"_(LOD\d+)$", re.IGNORECASE)
_DRAW = re.compile(r"^\s*drawindexed(?:instanced)?\s*=", re.IGNORECASE)
_INDEX_OFFSET = re.compile(r"^\s*match_first_index\s*=", re.IGNORECASE)
_OVERRIDE_POSITION = re.compile(r"^\s*vb0\s*=", re.IGNORECASE)
_OVERRIDE_TEXCOORD = re.compile(r"^\s*vb1\s*=", re.IGNORECASE)
_RESOURCE = "ResourceSSMTBoneMatrixRender"
_EPOCH = "$ssmt_bone_matrix_calibration_epoch"
_WAS_ACTIVE = "$ssmt_bone_matrix_calibration_active"
SKIN_SHADER = "zzmi_bone_matrix_override.hlsl"
_INDEX_COUNT = re.compile(r"^\s*match_index_count\s*=\s*[1-9]\d*\s*$", re.IGNORECASE)
_BIND_VERTEX = re.compile(
    r"^ResourceSSMTBoneMatrixBindPosition_([0-9a-f]{8})(?:_(LOD\d+))?$", re.IGNORECASE
)
_ARRAY = re.compile(r"^\s*array\s*=\s*([1-9]\d*)\s*$", re.IGNORECASE)
_FILENAME = re.compile(r"^\s*filename\s*=\s*(.+?)\s*$", re.IGNORECASE)
_HANDLING_SKIP = re.compile(r"^\s*handling\s*=\s*skip\s*$", re.IGNORECASE)
_ORIGINAL_VB = f"{_RESOURCE}OriginalVB"
_ORIGINAL_UV = f"{_RESOURCE}OriginalUV"
_PUBLISH_POSITION = re.compile(r"^\s*(Resource\w*Position(?:_LOD\d+)?)\s*=\s*(?:ref|copy)\s+cs-u5\s*$", re.I)
_POSITION_INPUT = re.compile(r"^\s*vb0\s*=\s*(?:ref\s+)?(Resource\w+)\s*$", re.I)
_FILTER = re.compile(r"^\s*filter_index\s*=\s*([1-9]\d*(?:\.\d+)?)\s*$", re.I)
BONE_DRAW_READY = "; SSMT bone matrix before draw"
BONE_SLOT = "; SSMT bone matrix slot "
SUPPORT_FILE = "SSMTBoneMatrixGroundSupport.buf"


@dataclass(frozen=True)
class RenderBinding:
    index: int
    ib: str
    lod: str
    slot: str
    palette: str
    seen: str
    sites: list[tuple[int, str]]
    live_position: bool

    @property
    def resource(self):
        return f"{_RESOURCE}{self.index}"

    @property
    def group(self):
        return self.lod, self.slot


def _sections(lines):
    ranges = [(index, header[1]) for index, line in enumerate(lines) if (header := _SECTION.fullmatch(line))]
    return [
        (name, index, ranges[i + 1][0] if i + 1 < len(ranges) else len(lines))
        for i, (index, name) in enumerate(ranges)
    ]


def _anchor_native_expressions_to_blend(lines):
    """Use the unchanged Blend buffer when native expression Position rotates."""
    sections = _sections(lines)
    by_name = {name.casefold(): (start, end) for name, start, end in sections}
    replacements, closures, extra = {}, set(), {}
    for name, start, end in sections:
        lowered = name.casefold()
        if not lowered.startswith("textureoverride_vb_"):
            continue
        selector = r"_position(?=_(?:lod\d+)$|$)"
        blend_section = re.sub(selector, "_blend", lowered)
        texcoord_section = re.sub(selector, "_texcoord", lowered)
        if blend_section == lowered or blend_section not in by_name:
            continue
        if not any("run = customshaderzzminativeexpression_" in line.casefold() for line in lines[start + 1:end]):
            continue
        blend_start, blend_end = by_name[blend_section]
        blend_body = lines[blend_start + 1:blend_end]
        if any(line.strip() and not line.lstrip().startswith(";") and not _IB.fullmatch(line)
               for line in blend_body):
            continue
        position_hash = next((i for i in range(start + 1, end) if _IB.fullmatch(lines[i])), None)
        blend_hash = next((i for i in range(blend_start + 1, blend_end) if _IB.fullmatch(lines[i])), None)
        if position_hash is None or blend_hash is None or lines[position_hash] == lines[blend_hash]:
            continue
        texcoord = by_name.get(texcoord_section)
        if texcoord is None:
            continue
        uv_start, uv_end = texcoord
        uv_hash = next((m[1] for line in lines[uv_start + 1:uv_end] if (m := _IB.fullmatch(line))), None)
        if uv_hash is None:
            continue
        filter_line = next((line for line in lines[uv_start + 1:uv_end]
                            if line.strip().casefold().startswith("filter_index")), None)
        if filter_line is not None:
            existing_filter = _FILTER.fullmatch(filter_line)
            if existing_filter is None:
                continue
            tag = existing_filter[1]
        else:
            tag = str(0x400000 | (int(uv_hash, 16) & 0x3fffff))
            extra[uv_start + 1] = (f"; {_TAG} begin expression texcoord filter",
                                   f"filter_index = {tag}", f"; {_TAG} end")
        replacements[position_hash] = (
            f"; {_TAG} begin hash|{lines[position_hash]}", lines[blend_hash], f"; {_TAG} end",
            f"; {_TAG} begin expression deform guard",
            f"if DRAW_TYPE == 1 && @so0 != 0 && @vb0 != 0 && vb1 == {tag}",
            f"; {_TAG} end",
        )
        replacements[blend_hash] = (
            f"; {_TAG} begin hash|{lines[blend_hash]}", lines[position_hash], f"; {_TAG} end",
        )
        closures.add(end)
    if not replacements:
        return lines
    output = []
    for index in range(len(lines) + 1):
        if index in closures:
            output.extend((f"; {_TAG} begin expression deform end", "endif", f"; {_TAG} end"))
        output.extend(extra.get(index, ()))
        if index < len(lines):
            output.extend(replacements.get(index, (lines[index],)))
    return output


def _deform_bindings(lines, sections, capacities):
    published_positions = {
        match[1].casefold() for line in lines if (match := _PUBLISH_POSITION.fullmatch(line))
    }
    bindings = {}
    for name, start, end in sections:
        if not name.casefold().startswith("textureoverride"):
            continue
        area = lines[start + 1:end]
        live_position = any(
            "run = customshaderzzminativeexpression_" in line.casefold()
            or ((position := _POSITION_INPUT.fullmatch(line)) and position[1].casefold() in published_positions)
            for line in area
        )
        occurrence = _OCCURRENCE.search("\n".join(area))
        if occurrence is None:
            continue
        palettes = defaultdict(set)
        for line in area:
            palette = _PALETTE.fullmatch(line)
            if palette and palette[1].casefold() in capacities:
                palettes[palette[2]].add(palette[1])
        for index in range(start + 1, end):
            capture = _CAPTURE.match(lines[index])
            if capture and len(palettes[capture[3]]) == 1:
                ib, lod, slot = capture[2].lower(), (capture[4] or "").lower(), capture[3]
                key = ib, lod, slot
                binding = bindings.setdefault(key, RenderBinding(
                    len(bindings), ib, lod, slot,
                    next(iter(palettes[capture[3]])),
                    f"${occurrence[1]}zz_ms_seen_{occurrence[2]}{slot}", [], live_position,
                ))
                binding.sites.append((index, capture[1]))
    return bindings.values()



def _asset_inputs(lines, sections):
    inputs = {}
    resources = {name.casefold() for name, _, _ in sections}
    for name, start, end in sections:
        bound = _BIND_VERTEX.fullmatch(name)
        if not bound:
            continue
        blend = name.replace("BindPosition", "BindBlend")
        if blend.casefold() not in resources:
            continue
        rows = next((int(m[1]) for line in lines[start + 1:end] if (m := _ARRAY.fullmatch(line))), 0)
        if rows > 0:
            inputs[bound[1].lower(), (bound[2] or "").lower()] = (name, blend, rows)
    return inputs


def read_ground_support(text, mod_directory, anchor, layers=None):
    """Find a grounded component among this export's bind buffers."""
    lines = text.splitlines()
    sections = _sections(lines)
    files = {}
    for name, start, end in sections:
        filename = next((m[1] for line in lines[start + 1:end] if (m := _FILENAME.fullmatch(line))), None)
        if filename:
            files[name.casefold()] = Path(mod_directory) / filename

    best = None
    for key, (position, blend, rows) in _asset_inputs(lines, sections).items():
        pos_file, blend_file = files.get(position.casefold()), files.get(blend.casefold())
        if not pos_file or not blend_file or not pos_file.is_file() or not blend_file.is_file():
            continue
        if pos_file.stat().st_size != rows * POSITION_STRIDE or blend_file.stat().st_size != rows * BLEND_STRIDE:
            continue
        blend_data = blend_file.read_bytes()
        if layers and layers.custom_fallback:
            blend_data = remap_blend_indices(blend_data, {
                "stride": BLEND_STRIDE,
                "elements": [
                    {"semantic": "BLENDINDICES", "offset": 16, "byte_width": 16, "format": "R32G32B32A32_UINT"},
                ],
            }, layers.native_capacity, layers.custom_fallback)
        support = ground_support_vertices(pos_file.read_bytes(), blend_data, anchor)
        if support:
            rank, payload = support
            if best is None or rank > best[0]:
                best = rank, key, payload
    return (best[1], best[2]) if best else (None, b"")


def prepare_native_blends(text, directory, native_capacity, layers, physics=None):
    """Prepare native fallback weights and identify draws with no simulated influences."""
    lines = text.splitlines()
    sections = _sections(lines)
    bodies = {name.casefold(): lines[start + 1:end] for name, start, end in sections}
    filenames = {}
    for name, body in bodies.items():
        if name.startswith("resource"):
            match = next((m for line in body if (m := _FILENAME.fullmatch(line))), None)
            if match:
                filenames[name] = match[1]
    directory = Path(directory)

    fallback = layers.custom_fallback if layers else ()
    fallback_files = {}
    draws = {}
    payloads = []
    unaffected = set()
    simulated_slots = {slot for slot, actor in enumerate(physics.followers) if actor >= 0} if physics else set()
    for key, (_, bind_blend, rows) in _asset_inputs(lines, sections).items():
        blend_name = bind_blend.casefold()
        path = filenames.get(blend_name)
        if not path:
            continue
        packed = (directory / path).read_bytes()
        slots = weighted_slots(packed, 0 if physics else native_capacity)
        if physics and len(packed) == rows * BLEND_STRIDE and slots.isdisjoint(simulated_slots):
            unaffected.add(key)
        extras = {slot for slot in slots if slot >= native_capacity} if physics else slots
        if not extras:
            continue
        for slot in sorted(extras):
            offset = slot - native_capacity
            if offset >= len(fallback) or fallback[offset] == 0xFFFFFFFF:
                raise ValueError(f"DrawIB {key[0]} 的顶点组 {slot} 缺少已挂接的新增骨骼")
        ib, lod = key
        resource = f"resource{ib}blend" + (f"_{lod}" if lod else "")
        src_file = filenames.get(resource)
        layout = next((line.strip()[len(BLEND_LAYOUT_MARKER):] for line in bodies[blend_name]
                       if line.strip().startswith(BLEND_LAYOUT_MARKER)), None)
        if not src_file or not layout:
            raise ValueError(f"DrawIB {ib} 缺少原生权重布局，请重新导出网格")
        src_path = (directory / src_file).resolve()
        draws[key] = {fallback[slot - native_capacity] for slot in extras}
        source = src_path.read_bytes()
        layout = json.loads(layout)
        if normalized_blend(source, layout) != packed:
            raise ValueError(f"DrawIB {ib} 的原生权重与扩展蒙皮缓冲不一致，请重新导出网格")
        if src_path in fallback_files:
            continue
        target = f"SSMTBoneMatrixNativeBlend_{ib}{'_' + lod if lod else ''}.buf"
        replaced = remap_blend_indices(source, layout, native_capacity, fallback)
        fallback_files[src_path] = f"res/{target}"
        payloads.append((target, replaced))
    replacements = {}
    if fallback_files:
        for name, path in filenames.items():
            if target := fallback_files.get((directory / path).resolve()):
                replacements[name] = target
    return replacements, draws, payloads, unaffected


def bind_native_blends(text, replacements):
    lines = text.splitlines()
    for name, start, end in reversed(_sections(lines)):
        if (target := replacements.get(name.casefold())) is None:
            continue
        index = next(i for i in range(start + 1, end) if _FILENAME.fullmatch(lines[i]))
        lines[index:index + 1] = [
            f"; {_TAG} begin blend|{lines[index]}",
            f"filename = {target}",
            f"; {_TAG} end",
        ]
    return "\n".join(lines) + "\n"


def _instance_guard(bindings, binding, source="@vb0", render_inputs=None):
    render_inputs = render_inputs or {}
    matched = {item.slot: "@" + render_inputs.get(item.slot, item.resource + "SO") for item in bindings}
    unique = "".join(
        f" && ({other.seen} == 0 || {source} !== {matched[other.slot]})"
        for other in bindings if other is not binding
    )
    return f"{binding.seen} > 0 && {source} === {matched[binding.slot]}{unique}"


def bone_draw_palettes(sections, lines):
    """Return the effective palettes for private bone skinning outputs at a draw."""
    inputs = {}
    for line in lines:
        match = re.fullmatch(r"\s*vb0\s*=\s*ref\s+(ResourceSSMTBoneMatrixRender\d+)Output", line, re.I)
        if not match:
            continue
        resource = match[1]
        palette = resource + "ResolvedPalette"
        if f"[{palette}]" not in sections:
            continue
        output = resource + "Output"
        for setting in sections.get(f"[{output}]", ()):
            if setting.strip().startswith(BONE_SLOT):
                inputs[output] = (int(setting.strip()[len(BONE_SLOT):]), palette)
                break
    return inputs


def _restore_cb1():
    return (
        f"if @{ORIGINAL_CB1} != 0",
        f"    vs-cb1 = ref {ORIGINAL_CB1}", f"    {ORIGINAL_CB1} = null", "endif",
    )


def _snapshot_draw(draw, bindings, snapshot, site, render_inputs):
    indent = draw[:len(draw) - len(draw.lstrip())]
    command = draw.strip()
    lines = [f"; {_TAG} begin draw|{command}", "if @vb0 != 0"]
    if bindings:
        for index, binding in enumerate(bindings):
            branch = "if" if index == 0 else "elif"
            lines.append(f"    {branch} {_instance_guard(bindings, binding, render_inputs=render_inputs)}")
            lines.extend(f"        {line}" for line in snapshot.draw(site, binding.index))
        lines.append("    endif")
    else:
        lines.extend(f"    {line}" for line in snapshot.draw(site, 0, match_vertex_buffer=True))
    lines.extend(("endif", BONE_DRAW_READY, command, *_restore_cb1(), f"; {_TAG} end"))
    return [indent + line for line in lines]


def _render_draw(draw, bindings, play, references, texcoord, snapshot, site, layers, physics, preview,
                 physics_indices, render_inputs):
    indent = draw[:len(draw) - len(draw.lstrip())]
    command = draw.strip()
    skin_enabled = f"{play} != 0"
    if physics:
        skin_enabled = f"({skin_enabled} || {physics.play} != 0 || {physics.debug} != 0)"
    enabled = f"({skin_enabled} || {snapshot.control.variable} != 0)" if snapshot else skin_enabled
    lines = [f"; {_TAG} begin draw|{command}", f"if {enabled} && @vb0 != 0"]
    for index, binding in enumerate(bindings):
        resource = binding.resource
        branch = "if" if index == 0 else "elif"
        lines.append(f"    {branch} {_instance_guard(bindings, binding, render_inputs=render_inputs)}")
        if snapshot:
            lines.extend(f"        {line}" for line in snapshot.draw(site, binding.index))
        if layers:
            lines.extend((f"        if {skin_enabled}", f"            run = {LAYER_UPDATE}", "        endif"))
        reference = references.get(binding.group)
        if reference is binding:
            capture = f"({play} != 0 || @{ORIGINAL_CB1} != 0)" if snapshot else f"{play} != 0"
            if layers:
                capture = f"{LAYER_FULL} != 0 && ({capture})"
            lines.extend((
                f"        if {capture}",
                f"            {resource}ObjectCB = copy vs-cb1",
                f"            if {resource}ObjectCB->size >= 64",
                f"                run = CustomShaderSSMTBoneMatrixCalibrate{binding.index}",
                "            endif",
                "        endif",
            ))
        pose = [f"{_ORIGINAL_VB} = ref vb0"]
        if texcoord:
            pose.extend((
                "if @vb1 != 0",
                f"    {_ORIGINAL_UV} = ref vb1",
                f"    vb1 = ref {texcoord}",
                "endif",
            ))
        if binding.index in physics_indices or (reference and reference is not binding):
            pose.append(f"{resource}ObjectCB = copy vs-cb1")
        pose.extend((
            f"run = CommandListSSMTBoneMatrixSkin{binding.index}",
            f"vb0 = ref {resource}Output",
        ))
        if snapshot:
            lines.extend((f"        if {skin_enabled}", *(f"            {line}" for line in pose), "        endif"))
        else:
            lines.extend(f"        {line}" for line in pose)
    lines.extend(("    endif", "endif", BONE_DRAW_READY, command))
    if preview:
        lines.append(f"if {physics.debug} != 0 && @{_ORIGINAL_VB} != 0 && @o0 != 0 && vs-cb0->size >= 2096")
        for index, binding in enumerate(preview):
            branch = "if" if index == 0 else "elif"
            lines.extend((
                f"    {branch} {_instance_guard(bindings, binding, '@' + _ORIGINAL_VB, render_inputs)}",
                f"        run = CommandListSSMTBonePhysicsPreview{binding.index}",
            ))
        lines.extend(("    endif", "endif"))
    lines.extend((
        f"if @{_ORIGINAL_VB} != 0",
        f"    vb0 = ref {_ORIGINAL_VB}", f"    {_ORIGINAL_VB} = null",
        "endif",
    ))
    if texcoord:
        lines.extend((
            f"if @{_ORIGINAL_UV} != 0",
            f"    vb1 = ref {_ORIGINAL_UV}", f"    {_ORIGINAL_UV} = null", "endif",
        ))
    if snapshot:
        lines.extend(_restore_cb1())
    lines.append(f"; {_TAG} end")
    return [indent + line for line in lines]


def _merged_skin_render_inputs(area, bindings, resources):
    """Identify the per-IB merged skin output already bound at this draw."""
    slot_by_output = {
        f"resourcezzskinout_{binding.ib}_s{binding.slot}{binding.lod}": binding.slot
        for binding in bindings
    }
    inputs = {}
    for line in area:
        if not _OVERRIDE_POSITION.match(line):
            continue
        match = _POSITION_INPUT.fullmatch(line)
        name = match[1].casefold() if match else ""
        if name not in slot_by_output or name not in resources:
            return None
        inputs[slot_by_output[name]] = match[1]
    return inputs


def _scoped_render_draws(lines, sections, bindings, assets):
    draw_ranges = indexed_draw_ranges(lines)
    render_by_ib = defaultdict(list)
    for binding in bindings:
        key = binding.ib, binding.lod
        if assets is None or key in assets:
            render_by_ib[key].append(binding)
    unmerged_snapshot = assets is None and not render_by_ib
    sites = {}
    render_skips = []
    resources = {name.casefold() for name, _, _ in sections}
    for name, start, end in sections:
        if not name.casefold().startswith("textureoverride"):
            continue
        area = lines[start + 1:end]
        ib = next((match[1].lower() for line in area if (match := _IB.fullmatch(line))), "")
        lod = _LOD_SUFFIX.search(name)
        candidates = render_by_ib.get((ib, lod[1].lower() if lod else ""), ())
        if not ib or (not candidates and not unmerged_snapshot) or not any(_INDEX_OFFSET.match(line) for line in area):
            continue
        if not any(_INDEX_COUNT.fullmatch(line) for line in area):
            continue
        render_inputs = _merged_skin_render_inputs(area, candidates, resources)
        if assets is not None and render_inputs is None:
            continue
        draw_sites = [index for index in range(start + 1, end) if index in draw_ranges or _DRAW.match(lines[index])]
        if not draw_sites:
            continue
        if not any(_HANDLING_SKIP.fullmatch(line) for line in area):
            render_skips.append(start + 1)
        uv = f"Resource{ib}Texcoord"
        if uv.casefold() not in resources or any(_OVERRIDE_TEXCOORD.match(line) for line in area):
            uv = None
        sites.update((index, (candidates, uv, render_inputs)) for index in draw_sites)
    return sites, render_skips


def _dispatch(shader_name, file, groups, arguments, params, suffix):
    saved = [(register, _RESOURCE + "Saved" + register.removeprefix("cs-")) for register, _ in arguments]
    return [
        f"[{shader_name}]", f"cs = res/{file}",
        *(f"local $ssmt_bm_{suffix}_{key} = {key}" for key, _ in params),
        *(f"{key} = {value}" for key, value in params),
        *(f"{resource} = ref {register}" for register, resource in saved),
        *(f"{register} = ref {value}" for register, value in arguments),
        f"Dispatch = {groups}, 1, 1",
        *(f"{register} = ref {resource}" for register, resource in reversed(saved)),
        *(f"{resource} = null" for _, resource in saved),
        *(f"{key} = $ssmt_bm_{suffix}_{key}" for key, _ in params),
        "",
    ]


def _physics_group_name(binding):
    return (binding.lod or "_base") + "_s" + binding.slot


def _physics_preview(index, group, state, physics):
    command = f"CustomShaderSSMTBonePhysicsPreview{index}"
    frame = "$ssmt_bone_physics_drawn" + group
    camera = "ResourceSSMTBonePhysicsCamera" + group
    output = "ResourceSSMTBonePhysicsPreviewTarget" + group
    view = "ResourceSSMTBonePhysicsPreviewView" + group
    registers = (("105", BODIES), ("106", state), ("107", camera),
                 ("108", JOINT_OPTIONS if physics.has_boxes else "null"))
    lines = [
        f"[CommandListSSMTBonePhysicsPreview{index}]",
        f"if {frame} != frame_number || @o0 !== @{output} || @vs-cb0 !== @{view}",
        f"    {frame} = frame_number", f"    {output} = ref o0", f"    {view} = ref vs-cb0",
        f"    {camera} = copy vs-cb0", f"    run = {command}", "endif", "",
        f"[{command}]", f"vs = res/{PREVIEW_SHADER}", f"ps = res/{PREVIEW_PIXEL_SHADER}",
        "gs = null", "hs = null", "ds = null", "topology = line_list",
        "depth_enable = false", "depth_write_mask = zero", "stencil_enable = false",
        "cull = none", "scissor_enable = false", "blend = ADD ONE INV_SRC_ALPHA",
        "alpha = ADD ZERO ONE",
        *(f"mask[{target}] = 0" for target in range(1, 8)),
    ]
    for slot, source in registers:
        lines.extend((f"ResourceSSMTBonePhysicsSavedVS{slot} = ref vs-t{slot}", f"vs-t{slot} = ref {source}"))
    lines.append(f"draw = {len(physics.capsules) * 378}, 0")
    for slot, _ in reversed(registers):
        lines.extend((f"vs-t{slot} = ref ResourceSSMTBonePhysicsSavedVS{slot}", f"ResourceSSMTBonePhysicsSavedVS{slot} = null"))
    lines.append("")
    return lines


def _resources(bindings, assets, frame, clip, references, support_key, snapshot, layers, capacities,
               physics, physics_references, physics_indices, preview_indices):
    registers = ("t90", "t91", "t92", "t93", "t94", "t95", "t96", "t98", "u7")
    if layers:
        registers += ("t100", "t101", "t102", "t103")
        if layers.custom_fallback:
            registers += ("t104",)
    if physics:
        registers += ("t99",)
        if physics.has_joint_options:
            registers += ("t108",)
    result = ["", f"; {_TAG} begin scoped render resources", f"[{_ORIGINAL_VB}]", "", f"[{_ORIGINAL_UV}]", ""]
    for register in (*registers, *(("t97", "u6") if references or physics else ())):
        result.extend((f"[{_RESOURCE}Saved{register}]", ""))
    if physics:
        result.extend(physics.definitions())
        for register in (105, 106, 107, 108):
            result.extend((f"[ResourceSSMTBonePhysicsSavedVS{register}]", ""))
        for group in sorted({_physics_group_name(binding) for binding in bindings}):
            result.extend((
                f"[ResourceSSMTBonePhysicsCamera{group}]", "type = Buffer", "stride = 16",
                "format = R32G32B32A32_FLOAT", "bind_flags = shader_resource", "",
                f"[ResourceSSMTBonePhysicsPreviewTarget{group}]", "",
                f"[ResourceSSMTBonePhysicsPreviewView{group}]", "",
                f"[ResourceSSMTBonePhysicsState{group}]", "type = RWBuffer",
                "format = R32G32B32A32_FLOAT", "bind_flags = shader_resource unordered_access",
                "filename = res/SSMTBonePhysicsStateInit.buf", "",
            ))
    for binding in bindings:
        position, blend, rows = assets[binding.ib, binding.lod]
        resource = binding.resource
        orientation = references.get(binding.group)
        result.extend((
            f"[{resource}SO]", "",
            f"[{resource}Output]", "type = Buffer", "stride = 40", f"array = {rows}",
            f"{BONE_SLOT}{binding.slot}",
            "bind_flags = unordered_access vertex_buffer shader_resource", "misc_flags = none", "",
            f"[{resource}Write]", "type = Buffer", "stride = 4", "format = R32_FLOAT", "",
        ))
        if binding.live_position:
            position = resource + "MorphedPosition"
            result.extend((
                f"[{position}]", "type = StructuredBuffer", "stride = 40",
                "bind_flags = shader_resource", "",
            ))
        if orientation or binding.index in physics_indices:
            result.extend((
                f"[{resource}ObjectCB]", "type = Buffer", "stride = 16",
                "format = R32G32B32A32_FLOAT", "bind_flags = shader_resource", "",
            ))
        if orientation is binding:
            support = f"{_RESOURCE}Support" if (binding.ib, binding.lod) == support_key else "null"
            result.extend((
                f"[{resource}World]", "type = RWBuffer", "format = R32G32B32A32_FLOAT",
                "bind_flags = shader_resource unordered_access",
                "filename = res/SSMTBoneMatrixWorldInit.buf", "",
                f"[{resource}Basis]", "type = RWBuffer", "format = R32G32B32A32_FLOAT",
                "bind_flags = shader_resource unordered_access",
                "filename = res/SSMTBoneMatrixReferenceBasis.buf", "",
            ))
            snapshot_frame = f"(({SNAPSHOT_FRAME} + 1) * (@{ORIGINAL_CB1} != 0)) - 1" if snapshot else "-1"
            result.extend(_dispatch(
                f"CustomShaderSSMTBoneMatrixCalibrate{binding.index}", RENDER_SHADER, 1,
                (
                    ("cs-t90", resource + "ObjectCB"), ("cs-t91", binding.palette),
                    ("cs-t92", "ResourceSSMTBoneMatrixClip"), ("cs-t93", _RESOURCE + "Pivot"),
                    ("cs-t94", support), ("cs-u6", resource + "Basis"), ("cs-u7", resource + "World"),
                ),
                (("x42", _EPOCH), ("y42", "frame_number"), ("z42", snapshot_frame), ("w42", "34674")), "cal",
            ))
        index = binding.index
        palette = binding.palette
        skin_commands = []
        if layers:
            palette = resource + "ResolvedPalette"
            write_palette = palette + "Write"
            bone_count = layers.capacity if layers.custom_fallback else capacities[binding.palette.casefold()]
            result.extend((
                f"[{palette}]", "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"array = {bone_count * 3}", "bind_flags = shader_resource unordered_access", "",
                f"[{write_palette}]", "type = Buffer", "stride = 4", "format = R32_FLOAT", "",
            ))
            resolve_inputs = [
                ("cs-t92", binding.palette), ("cs-t93", "ResourceSSMTBoneMatrixTracks"),
                ("cs-t95", "ResourceSSMTBoneMatrixClip"), ("cs-t100", "ResourceSSMTBoneMatrixLayerRanges"),
                ("cs-t101", "ResourceSSMTBoneMatrixLayerCandidates"),
                ("cs-t102", "ResourceSSMTBoneMatrixLayerTracks"), ("cs-t103", LAYER_CONTROL),
            ]
            resolve_params = [("y41", f"2 + {LAYER_FULL}" if orientation else "2"), ("w41", "34673")]
            if orientation:
                resolve_inputs.extend((("cs-t96", resource + "ObjectCB"), ("cs-t97", orientation.resource + "World")))
                resolve_params.append(("z41", _EPOCH))
            if layers.custom_fallback:
                resolve_inputs.append(("cs-t104", LAYER_FALLBACK))
            resolve_inputs.append(("cs-u7", write_palette))
            resolve_shader = f"CustomShaderSSMTBoneMatrixResolveLayers{index}"
            result.extend(_dispatch(
                resolve_shader, SKIN_SHADER, (bone_count + 63) // 64, resolve_inputs, resolve_params, "layers",
            ))
            skin_commands.extend((f"{write_palette} = ref {palette}", f"run = {resolve_shader}"))
            if index in physics_indices:
                group = _physics_group_name(binding)
                state = "ResourceSSMTBonePhysicsState" + group
                physics_shader = f"CustomShaderSSMTBonePhysics{index}"
                solver_params = tuple((key, str(value)) for key, value in
                                      zip(("x48", "y48", "z48", "w48"), physics.solver_params))
                physics_inputs = [
                    ("cs-t96", resource + "ObjectCB"), ("cs-t97", BODIES), ("cs-t98", FOLLOWERS),
                    ("cs-t99", AIR_DRAG), ("cs-u6", state), ("cs-u7", write_palette),
                ]
                if physics.has_joint_options:
                    physics_inputs.append(("cs-t108", JOINT_OPTIONS))
                result.extend(_dispatch(
                    physics_shader, PHYSICS_SHADER, 1, physics_inputs,
                    (("x46", "time"), ("y46", "frame_number"), ("z46", RESET),
                     ("w46", str(physics.gravity)), ("x47", str(physics.repulsion)),
                     ("y47", f"({physics.play} != 0)"),
                     ("z47", "0" if binding is physics_references[binding.group] else "-1"),
                     ("w47", str(JOINT_PARAMS_TAG if physics.has_joint_options else AIR_PARAMS_TAG)))
                    + solver_params, "physics",
                ))
                if index in preview_indices:
                    result.extend(_physics_preview(index, group, state, physics))
                skin_commands.extend((
                    f"if ({physics.play} != 0 || {physics.debug} != 0) && {resource}ObjectCB->size >= 64",
                    f"    run = {physics_shader}", "endif",
                ))
        result.extend((
            f"[CommandListSSMTBoneMatrixSkin{index}]",
            *skin_commands,
            f"{resource}Write = ref {resource}Output",
            f"run = CustomShaderSSMTBoneMatrixSkin{index}",
            "",
        ))
        arguments = [
            ("cs-t90", position), ("cs-t91", blend), ("cs-t92", palette),
            ("cs-t93", "ResourceSSMTBoneMatrixTracks"), ("cs-t94", "ResourceSSMTBoneMatrixSlotMap"),
            ("cs-t95", "ResourceSSMTBoneMatrixClip"), ("cs-t98", "ResourceSSMTBoneMatrixParents"),
        ]
        if orientation and not layers:
            arguments.extend((("cs-t96", resource + "ObjectCB"), ("cs-t97", orientation.resource + "World")))
        arguments.append(("cs-u7", resource + "Write"))
        params = [("y41", "4")] if layers else [
            ("x41", f"{frame} - ({clip.first_frame})"), ("y41", str(int(orientation is not None))),
        ]
        if orientation and not layers:
            params.append(("z41", _EPOCH))
        params.append(("w41", "34673"))
        result.extend(_dispatch(
            f"CustomShaderSSMTBoneMatrixSkin{index}", SKIN_SHADER, (rows + 63) // 64, arguments,
            params, "skin",
        ))
    if references:
        result.extend((
            f"[{_RESOURCE}Pivot]", "type = Buffer", "format = R32G32B32A32_FLOAT",
            "filename = res/SSMTBoneMatrixBindPivot.buf", "",
        ))
    if support_key:
        result.extend((
            f"[{_RESOURCE}Support]", "type = StructuredBuffer", "stride = 48",
            "bind_flags = shader_resource", f"filename = res/{SUPPORT_FILE}", "",
        ))
    result.append(f"; {_TAG} end")
    return result


def add_scoped_playback(text, play, frame, clip, capacities, native_slots, support_key=None, cb1=None, layers=None,
                        extra_draws=None, physics=None, unaffected_physics_draws=()):
    lines = text.splitlines()
    if clip:
        lines = _anchor_native_expressions_to_blend(lines)
    sections = _sections(lines)
    assets = _asset_inputs(lines, sections) if clip else None
    bindings = _deform_bindings(lines, sections, capacities)
    full = clip is not None and clip.pivot is not None and clip.slots == tuple(range(native_slots))
    if layers:
        full = bool(layers.coverage)
        play = layers.enabled
    sites, render_skips = _scoped_render_draws(lines, sections, bindings, assets)
    used = {binding.index for candidates, _, _ in sites.values() for binding in candidates}
    chosen = [binding for binding in bindings if binding.index in used]
    extra_draws = extra_draws or {}
    unsupported = set(extra_draws) - {(binding.ib, binding.lod) for binding in chosen}
    if unsupported:
        ib, _ = min(unsupported)
        raise ValueError(f"DrawIB {ib} 的新增骨骼缺少可隔离的蒙皮绘制，请检查该部件的重放绑定")
    for binding in chosen:
        parents = extra_draws.get((binding.ib, binding.lod), ())
        if parents and max(parents) >= capacities[binding.palette.casefold()]:
            raise ValueError(f"DrawIB {binding.ib} 的新增骨骼挂接到此绘制骨骼池之外的原生骨骼")
    if not sites:
        raise ValueError("骨骼矩阵控制没有找到可按 IB、起始索引和索引数隔离的 ZZMI 渲染绘制")
    insertions = {}
    references = {}
    if full:
        for binding in chosen:
            previous = references.get(binding.group)
            key = binding.ib, binding.lod
            if previous is None or (key == support_key) or (
                (previous.ib, previous.lod) != support_key
                and assets[key][2] > assets[previous.ib, previous.lod][2]
            ):
                references[binding.group] = binding
    preview = {}
    physics_references = {}
    physics_indices = set()
    if physics:
        for binding in chosen:
            key = binding.ib, binding.lod
            previous = physics_references.get(binding.group)
            if previous is None or assets[key][2] > assets[previous.ib, previous.lod][2]:
                physics_references[binding.group] = binding
            rank = key in extra_draws, assets[key][2]
            if binding.group not in preview or preview[binding.group][0] < rank:
                preview[binding.group] = (rank, binding.index)
            if key not in unaffected_physics_draws:
                physics_indices.add(binding.index)
        physics_references.update(references)
        # One binding per group still advances the simulation, even when its weights are static.
        physics_indices.update(binding.index for binding in physics_references.values())
    preview_indices = {entry[1] for entry in preview.values()}
    physics_indices.update(preview_indices)
    snapshot = SnapshotSections(cb1) if cb1 else None
    if clip:
        replacements = {
            index: _render_draw(
                lines[index], candidates,
                "1" if (candidates[0].ib, candidates[0].lod) in extra_draws else play,
                references, texcoord, snapshot, site, layers, physics,
                [binding for binding in candidates if binding.index in preview_indices],
                physics_indices, render_inputs,
            )
            for site, (index, (candidates, texcoord, render_inputs)) in enumerate(sites.items())
        }
    else:
        replacements = {
            index: _snapshot_draw(lines[index], candidates, snapshot, site, render_inputs)
            for site, (index, (candidates, _, render_inputs)) in enumerate(sites.items())
        }
    for index in render_skips:
        insertions[index] = [f"; {_TAG} begin skip original draw", "handling = skip", f"; {_TAG} end"]
    for binding in chosen:
        for index, indent in binding.sites:
            captures = (
                f"; {_TAG} begin instance association",
                f"{binding.resource}SO = ref so0",
            )
            if clip and binding.live_position:
                captures += (f"{binding.resource}MorphedPosition = copy vb0",)
            insertions[index + 1] = [indent + command for command in (*captures, f"; {_TAG} end")]
    if full or snapshot or physics:
        constants = []
        present = []
        if full:
            constants.append(f"global {_EPOCH} = 0")
            if not layers:
                constants.append(f"global {_WAS_ACTIVE} = 0")
                lock_off = f" && {cb1.variable} == 0" if cb1 else ""
                present.extend((
                    f"{_EPOCH} = {_EPOCH} + ({_WAS_ACTIVE} != 0 && {play} == 0{lock_off})",
                    f"{_WAS_ACTIVE} = ({play} != 0)",
                ))
        if snapshot:
            constants.extend(snapshot.globals)
            present.extend(snapshot.present)
        if physics:
            constants.extend(f"global $ssmt_bone_physics_drawn{group} = -1"
                             for group in sorted({_physics_group_name(binding) for binding in chosen}))
        found_present = False
        for name, index, _ in sections:
            if name.casefold() == "constants":
                insertions[index + 1] = [f"; {_TAG} begin render constants", *constants, f"; {_TAG} end"]
            elif name.casefold() == "present":
                insertions[index + 1] = [f"; {_TAG} begin render present", *present, f"; {_TAG} end"]
                found_present = True
        if not found_present and present:
            lines.extend((f"; {_TAG} begin render present", "[Present]", *present, f"; {_TAG} end"))
    output = []
    for index, line in enumerate(lines):
        output.extend(insertions.get(index, ()))
        output.extend(replacements.get(index, (line,)))
    if clip:
        output.extend(_resources(
            chosen, assets, frame, clip, references, support_key if references else None, snapshot, layers, capacities,
            physics, physics_references, physics_indices, preview_indices,
        ))
    elif chosen:
        output.extend(("", f"; {_TAG} begin snapshot bindings"))
        for binding in chosen:
            output.extend((f"[{binding.resource}SO]", ""))
        output.append(f"; {_TAG} end")
    if snapshot:
        output.extend(snapshot.definitions())
    if layers:
        output.extend(layers.definitions(SKIN_SHADER))
    return "\n".join(output) + "\n"
