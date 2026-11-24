"""Build a private skinning pass scoped to an exact ZZMI render override."""

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .zzmi_bone_matrix_layers import LAYER_CONTROL, LAYER_FALLBACK, LAYER_FULL, LAYER_UPDATE
from .zzmi_bone_matrix_render_inputs import (
    BLEND_LAYOUT_MARKER, BLEND_STRIDE, POSITION_STRIDE, extra_weighted_slots,
    ground_support_vertices, normalized_blend, remap_blend_indices,
)
from .zzmi_bone_matrix_snapshot import ORIGINAL_CB1, SNAPSHOT_FRAME, SnapshotSections

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
    native_expression: bool

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


def _deform_bindings(lines, sections, capacities):
    bindings = {}
    for name, start, end in sections:
        if not name.casefold().startswith("textureoverride"):
            continue
        area = lines[start + 1:end]
        native_expression = any("run = customshaderzzminativeexpression_" in line.casefold() for line in area)
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
                    f"${occurrence[1]}zz_ms_seen_{occurrence[2]}{slot}", [], native_expression,
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


def prepare_native_blends(text, directory, native_capacity, layers):
    """Make ordinary game draws bind the nearest native parent for custom weights."""
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
    for key, (_, bind_blend, _) in _asset_inputs(lines, sections).items():
        blend_name = bind_blend.casefold()
        path = filenames.get(blend_name)
        if not path:
            continue
        packed = (directory / path).read_bytes()
        extras = extra_weighted_slots(packed, native_capacity)
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
    return replacements, draws, payloads


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


def _instance_guard(bindings, binding):
    unique = "".join(
        f" && ({other.seen} == 0 || @vb0 !== @{other.resource}SO)"
        for other in bindings if other is not binding
    )
    return f"{binding.seen} > 0 && @vb0 === @{binding.resource}SO{unique}"


def _restore_cb1():
    return (
        f"if @{ORIGINAL_CB1} != 0",
        f"    vs-cb1 = ref {ORIGINAL_CB1}", f"    {ORIGINAL_CB1} = null", "endif",
    )


def _snapshot_draw(draw, bindings, snapshot, site):
    indent = draw[:len(draw) - len(draw.lstrip())]
    command = draw.strip()
    lines = [f"; {_TAG} begin draw|{command}", "if @vb0 != 0"]
    if bindings:
        for index, binding in enumerate(bindings):
            branch = "if" if index == 0 else "elif"
            lines.append(f"    {branch} {_instance_guard(bindings, binding)}")
            lines.extend(f"        {line}" for line in snapshot.draw(site, binding.index))
        lines.append("    endif")
    else:
        lines.extend(f"    {line}" for line in snapshot.draw(site, 0, match_vertex_buffer=True))
    lines.extend(("endif", command, *_restore_cb1(), f"; {_TAG} end"))
    return [indent + line for line in lines]


def _render_draw(draw, bindings, play, references, texcoord, snapshot, site, layers):
    indent = draw[:len(draw) - len(draw.lstrip())]
    command = draw.strip()
    enabled = f"({play} != 0 || {snapshot.control.variable} != 0)" if snapshot else f"{play} != 0"
    lines = [f"; {_TAG} begin draw|{command}", f"if {enabled} && @vb0 != 0"]
    for index, binding in enumerate(bindings):
        resource = binding.resource
        branch = "if" if index == 0 else "elif"
        lines.append(f"    {branch} {_instance_guard(bindings, binding)}")
        if snapshot:
            lines.extend(f"        {line}" for line in snapshot.draw(site, binding.index))
        if layers:
            lines.extend((f"        if {play} != 0", f"            run = {LAYER_UPDATE}", "        endif"))
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
        if reference and reference is not binding:
            pose.append(f"{resource}ObjectCB = copy vs-cb1")
        pose.extend((
            f"run = CommandListSSMTBoneMatrixSkin{binding.index}",
            f"vb0 = ref {resource}Output",
        ))
        if snapshot:
            lines.extend((f"        if {play} != 0", *(f"            {line}" for line in pose), "        endif"))
        else:
            lines.extend(f"        {line}" for line in pose)
    lines.extend((
        "    endif", "endif", command,
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


def _scoped_render_draws(lines, sections, bindings, assets):
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
        if assets is not None and any(_OVERRIDE_POSITION.match(line) for line in area):
            continue
        draw_sites = [index for index in range(start + 1, end) if _DRAW.match(lines[index])]
        if not draw_sites:
            continue
        if not any(_HANDLING_SKIP.fullmatch(line) for line in area):
            render_skips.append(start + 1)
        uv = f"Resource{ib}Texcoord"
        if uv.casefold() not in resources or any(_OVERRIDE_TEXCOORD.match(line) for line in area):
            uv = None
        sites.update((index, (candidates, uv)) for index in draw_sites)
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


def _resources(bindings, assets, frame, clip, references, support_key, snapshot, layers, capacities):
    registers = ("t90", "t91", "t92", "t93", "t94", "t95", "t96", "t98", "u7")
    if layers:
        registers += ("t100", "t101", "t102", "t103")
        if layers.custom_fallback:
            registers += ("t104",)
    result = ["", f"; {_TAG} begin scoped render resources", f"[{_ORIGINAL_VB}]", "", f"[{_ORIGINAL_UV}]", ""]
    for register in (*registers, *(("t97", "u6") if references else ())):
        result.extend((f"[{_RESOURCE}Saved{register}]", ""))
    for binding in bindings:
        position, blend, rows = assets[binding.ib, binding.lod]
        resource = binding.resource
        orientation = references.get(binding.group)
        result.extend((
            f"[{resource}SO]", "",
            f"[{resource}Output]", "type = Buffer", "stride = 40", f"array = {rows}",
            "bind_flags = unordered_access vertex_buffer shader_resource", "misc_flags = none", "",
            f"[{resource}Write]", "type = Buffer", "stride = 4", "format = R32_FLOAT", "",
        ))
        if binding.native_expression:
            position = resource + "MorphedPosition"
            result.extend((
                f"[{position}]", "type = StructuredBuffer", "stride = 40",
                "bind_flags = shader_resource", "",
            ))
        if orientation:
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


def add_scoped_playback(text, play, frame, clip, capacities, support_key=None, cb1=None, layers=None, extra_draws=None):
    lines = text.splitlines()
    sections = _sections(lines)
    assets = _asset_inputs(lines, sections) if clip else None
    bindings = _deform_bindings(lines, sections, capacities)
    full = clip is not None and clip.pivot is not None and clip.slots == tuple(range(max(capacities.values(), default=0)))
    if layers:
        full = bool(layers.coverage)
        play = layers.enabled
    sites, render_skips = _scoped_render_draws(lines, sections, bindings, assets)
    used = {binding.index for candidates, _ in sites.values() for binding in candidates}
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
    snapshot = SnapshotSections(cb1) if cb1 else None
    if clip:
        replacements = {
            index: _render_draw(
                lines[index], candidates,
                "1" if (candidates[0].ib, candidates[0].lod) in extra_draws else play,
                references, texcoord, snapshot, site, layers,
            )
            for site, (index, (candidates, texcoord)) in enumerate(sites.items())
        }
    else:
        replacements = {
            index: _snapshot_draw(lines[index], candidates, snapshot, site)
            for site, (index, (candidates, _)) in enumerate(sites.items())
        }
    for index in render_skips:
        insertions[index] = [f"; {_TAG} begin skip original draw", "handling = skip", f"; {_TAG} end"]
    for binding in chosen:
        for index, indent in binding.sites:
            captures = (
                f"; {_TAG} begin instance association",
                f"{binding.resource}SO = ref so0",
            )
            if clip and binding.native_expression:
                captures += (f"{binding.resource}MorphedPosition = copy vb0",)
            insertions[index + 1] = [indent + command for command in (*captures, f"; {_TAG} end")]
    if full or snapshot:
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
