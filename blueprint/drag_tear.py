"""Optional render-only holes for the existing ZZMI drag interaction."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from .drag_cloth import _Assets, _field, prune_assets
from .drag_tear_uv import MASK_SIZE, rasterize_reference, read_uv

FAMILY = "drag_tear"
SHADER_DIR = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction"
BEGIN = "; --- SSMT TEAR BEGIN ---"
END = "; --- SSMT TEAR END ---"
DRAW = re.compile(r"drawindexed\s*=", re.IGNORECASE)
RUN = re.compile(r"run\s*=\s*(\S+)$", re.IGNORECASE)
TTL = re.compile(r"run\s*=\s*(CommandList\\TTL\\Draw|CommandListSSMTTTLDraw_\S+)\s*$", re.IGNORECASE)
TTL_DRAW = r"commandlist\ttl\draw"
TTL_TAKEN = "; [SSMT TTL 已接管]"
TTL_MASK = r"Resource\TTL\TransparencyTex"
TTL_INVERT = r"$\TTL\mask_invert"
MASK_MAGIC = 7312402
PIXEL_SLOT = "ps-t114"


def _clean(lines):
    inside = False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
        elif line.strip() == END:
            inside = False
        elif not inside:
            yield line


def clean_existing(sections, tail, ns):
    prefixes = ("ResourceDragTear", "CustomShaderDragTear", "CommandListDragTear", "KeyDragTear", "ShaderRegexDragTear")
    for name, lines in list(sections.items()):
        base = name[1:-1].split(".", 1)[0]
        if base.startswith(prefixes) and base.endswith(f"_{ns}"):
            del sections[name]
        else:
            sections[name] = list(_clean(lines))
    return "".join(_clean(tail.splitlines(keepends=True)))


def _part_owners(root, sections, part, vertex_count, names, name_key):
    lines = sections.get(f"[{part['ib_resource']}]", ())
    filename = _field(lines, "filename")
    index_format = str(_field(lines, "format")).upper().removeprefix("DXGI_FORMAT_")
    if not filename or index_format != "R32_UINT":
        return None
    try:
        path = Path(root, filename.replace("\\", "/"))
        if path.stat().st_size % 12:
            return None
        indices = np.fromfile(path, dtype="<u4").reshape(-1, 3)
    except OSError:
        return None
    if not len(indices) or len(indices) > 65535 * 64:
        return None

    owners = np.zeros(len(indices), dtype="<u4")
    claimed = np.zeros(len(indices), dtype="<u4")
    ids = {}
    for offset, count, name in part["name_ranges"]:
        if offset % 3 or count % 3 or offset + count > indices.size:
            continue
        key = name_key(name)
        if not key:
            continue
        object_id = ids.setdefault(key, len(ids) + 1)
        tri = slice(offset // 3, (offset + count) // 3)
        overlap = (claimed[tri] != 0) & (claimed[tri] != object_id)
        claimed[tri] = np.where(overlap, 0xFFFFFFFF, object_id)
        owners[tri] = np.where(overlap | (key not in names), 0, object_id)
    owners[np.any(indices >= vertex_count, axis=1)] = 0
    return (filename, indices, owners) if owners.any() else None


def asset_token(ns, ini_name):
    return hashlib.sha256(f"{ns}\0{ini_name}".encode("utf8")).hexdigest()[:20]


def bake(node, root, sections, components, ns, ini_name, names, name_key):
    if not names:
        return None
    token = asset_token(ns, ini_name)
    assets = None
    for component_id, comp in enumerate(components, 1):
        if not any(name_key(name) in names for part in comp["parts"] for _, _, name in part["name_ranges"]):
            continue
        positions = node._read_position_buf(root, sections, comp, comp["vertex_count"])
        if positions is None:
            continue
        cn = comp["comp_name"]
        for part_id, part in enumerate(comp["parts"]):
            if node._part_vertex_base(comp, part) != comp["vertex_base"]:
                continue
            data = _part_owners(root, sections, part, len(positions), names, name_key)
            if data is None:
                continue
            uv = read_uv(root, sections, comp, part, len(positions))
            if uv is None:
                continue
            filename, indices, owners = data
            masks = {}
            for owner in np.unique(owners[owners != 0]):
                atlas = rasterize_reference(positions, uv, indices[owners == owner])
                if not atlas[:, :, 3].any():
                    owners[owners == owner] = 0
                    continue
                if assets is None:
                    assets = _Assets(root, token, ns, family=FAMILY)
                masks[int(owner)] = assets.put(f"{cn}-p{part_id}-uv{owner}.buf", atlas)
            if not masks:
                continue
            header = np.array([component_id, part_id], dtype="<u4")
            owners_file = assets.put(f"{cn}-p{part_id}-owners.buf", np.concatenate((header, owners)))
            part["tear"] = (filename, owners, owners_file, masks)
            comp["tear_token"] = component_id
        if "tear_token" in comp:
            comp["tear_rest"] = assets.put(f"{cn}-rest.buf", positions.astype("<f4", copy=False))
    return assets


def _emit_part(sections, comp, part_id, ns, gesture_shader):
    part = comp["parts"][part_id]
    original_file, _, owners_file, _ = part["tear"]
    tag = f"{comp['comp_name']}P{part_id}_{ns}"
    resource = "ResourceDragTear"
    rest = f"{resource}Rest_{comp['comp_name']}_{ns}"
    original, faces, gesture = (
        f"{resource}{name}_{tag}" for name in ("Original", "Faces", "Gesture")
    )
    base = ["type = Buffer", "format = R32_UINT", f"filename = {original_file}"]
    sections[f"[{rest}]"] = ["type = StructuredBuffer", "stride = 12", f"filename = {comp['tear_rest']}"]
    sections[f"[{original}]"] = base
    sections[f"[{faces}]"] = ["type = Buffer", "format = R32_UINT", f"filename = {owners_file}"]
    sections[f"[{gesture}]"] = ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 4"]
    shared = [f"cs-t108 = {rest}", f"cs-t109 = {original}", f"cs-t110 = {faces}"]
    step = f"CustomShaderDragTearStep_{tag}"
    sections[f"[{step}]"] = [
        f"cs = {gesture_shader}", f"cs-t106 = ResourceDragPinnedDetectInfo_{ns}",
        f"cs-t107 = ResourceDragJiggleScreenState_{ns}", *shared,
        f"cs-u0 = {gesture}", "dispatch = 1, 1, 1", "cs-u0 = null",
    ]
    return tag, gesture, step


def _mask_texture(format):
    return [
        "type = Texture2D", f"width = {MASK_SIZE}", f"height = {MASK_SIZE}",
        "mips = 1", f"format = {format}", "bind_flags = shader_resource unordered_access",
    ]


def _emit_mask(sections, tag, owner, atlas, gesture, shader):
    mask = f"ResourceDragTearMask_{tag}"
    reference = f"ResourceDragTearUV_{tag}"
    paint = f"CustomShaderDragTearPaint_{tag}"
    sections[f"[{mask}]"] = _mask_texture("R32_FLOAT")
    sections[f"[{reference}]"] = ["type = Buffer", "format = R32G32_UINT", f"filename = {atlas}"]
    sections[f"[{paint}]"] = [
        f"cs = {shader}", f"cs-t106 = {gesture}", f"cs-t108 = {reference}",
        f"cs-u0 = {mask}", f"y494 = {owner}", f"dispatch = {MASK_SIZE // 8}, {MASK_SIZE // 8}, 1",
        "cs-u0 = null",
    ]
    return mask, paint


def _emit_pixel_regex(sections, ns):
    name = f"ShaderRegexDragTear_{ns}"
    sections[f"[{name}]"] = ["shader_model = ps_5_0", "temps = tearuv tearsize"]
    sections[f"[{name}.Pattern]"] = [
        r"(?m)\A(?![\s\S]*?^dcl_resource_\w+[^\r\n]*\bt114\b)"
        r"(?=[\s\S]*?^dcl_input_ps linear(?: \w+)? v0\.xyz?w?\r?$)"
        r"(?<head>[\s\S]*?^dcl_temps \d+\r?\n(?:^dcl_[^\r\n]*\r?\n)*)",
    ]
    sections[f"[{name}.Pattern.Replace]"] = ["${head}", *(line + r"\n" for line in (
        "ld_indexable(texture1d)(float,float,float,float) ${tearuv}.x, l(494, 0, 0, 0), t120.xxxx",
        f"eq ${{tearuv}}.x, ${{tearuv}}.x, l({MASK_MAGIC}.0)",
        "if_nz ${tearuv}.x",
        "resinfo_indexable(texture2d)(float,float,float,float)_uint ${tearsize}.xy, l(0), t114.xyzw",
        "utof ${tearsize}.xy, ${tearsize}.xyxx",
        "frc ${tearuv}.xy, v0.xyxx",
        "mul ${tearuv}.xy, ${tearuv}.xyxx, ${tearsize}.xyxx",
        "ftou ${tearuv}.xy, ${tearuv}.xyxx",
        "mov ${tearuv}.zw, l(0, 0, 0, 0)",
        "ld_indexable(texture2d)(float,float,float,float) ${tearuv}.x, ${tearuv}.xyzw, t114.xxxx",
        "ge ${tearuv}.x, ${tearuv}.x, l(0.5)",
        "discard_nz ${tearuv}.x",
        "endif",
    ))]
    sections[f"[{name}.InsertDeclarations]"] = [
        "dcl_resource_texture1d (float,float,float,float) t120",
        "dcl_resource_texture2d (float,float,float,float) t114",
    ]


def _emit_ttl(sections, tags, ns, epoch, shader):
    saved_ttl = f"ResourceDragTearSavedTTL_{ns}"
    sections[f"[{saved_ttl}]"] = []
    sections[f"[CommandListDragTearTTLRestore_{ns}]"] = [
        f"{TTL_MASK} = ref {saved_ttl}", f"{saved_ttl} = null",
    ]
    for tag, mask in tags.items():
        combined = f"ResourceDragTearTTL_{tag}"
        previous = f"ResourceDragTearTTLInput_{tag}"
        version = f"$ssmtdragtear_ttl_epoch_{tag}"
        invert = f"$ssmtdragtear_ttl_invert_{tag}"
        compose = f"CustomShaderDragTearTTLCompose_{tag}"
        sections[f"[{combined}]"] = _mask_texture("R32G32B32A32_FLOAT")
        sections[f"[{previous}]"] = []
        sections["[Constants]"].extend([BEGIN, f"global {version} = -1", f"global {invert} = -1", END])
        sections[f"[{compose}]"] = [
            f"cs = {shader}", f"cs-t106 = ref {mask}", f"cs-t107 = ref {saved_ttl}",
            f"cs-u0 = {combined}", f"dispatch = {MASK_SIZE // 8}, {MASK_SIZE // 8}, 1", "cs-u0 = null",
        ]
        sections[f"[CommandListDragTearTTLBind_{tag}]"] = [
            f"{saved_ttl} = ref {TTL_MASK}",
            f"if {version} != {epoch} || {invert} != {TTL_INVERT} || @{previous} != @{saved_ttl}",
            "\tlocal $ssmtdragtear_saved_invert = z494", f"\tz494 = {TTL_INVERT}",
            f"\tResourceDragTearSavedCsT106_{ns} = ref cs-t106",
            f"\tResourceDragTearSavedCsT107_{ns} = ref cs-t107",
            f"\trun = {compose}",
            f"\tcs-t106 = ref ResourceDragTearSavedCsT106_{ns}",
            f"\tcs-t107 = ref ResourceDragTearSavedCsT107_{ns}",
            f"\tResourceDragTearSavedCsT106_{ns} = null",
            f"\tResourceDragTearSavedCsT107_{ns} = null",
            "\tz494 = $ssmtdragtear_saved_invert",
            f"\t{version} = {epoch}", f"\t{invert} = {TTL_INVERT}",
            f"\t{previous} = ref {saved_ttl}", "endif",
            f"{TTL_MASK} = ref {combined} unless_null",
        ]


def _wrap(lines, sites, guard, ns):
    saved = f"ResourceDragTearSavedPS_{ns}"
    magic = f"$ssmtdragtear_saved_magic_{ns}"
    for index, (mask, ttl) in sorted(sites.items(), reverse=True):
        indent = re.match(r"[ \t]*", lines[index]).group()
        before = [BEGIN, guard, f"\t{saved} = ref {PIXEL_SLOT}",
                  f"\t{magic} = x494",
                  f"\t{PIXEL_SLOT} = ref {mask}", f"\tx494 = {MASK_MAGIC}"]
        after = [BEGIN, guard, f"\tx494 = {magic}",
                 f"\t{PIXEL_SLOT} = ref {saved}", f"\t{saved} = null"]
        if ttl:
            before.append(f"\trun = CommandListDragTearTTLBind_{ttl}")
            after.append(f"\trun = CommandListDragTearTTLRestore_{ns}")
        before.extend(("endif", END))
        after.extend(("endif", END))
        lines[index + 1:index + 1] = [indent + line for line in after]
        lines[index:index] = [indent + line for line in before]


def _wrap_ttl(lines, wrappers, guard, ns):
    name = ""
    sites = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].casefold()
        if name in wrappers and re.fullmatch(r"run\s*=\s*CommandList\\TTL\\Draw", stripped, re.IGNORECASE):
            choices = wrappers[name]
            if len(choices) == 1:
                sites[index] = next(iter(choices.values()))
    _wrap(lines, sites, guard, ns)


def _live_draw_index(lines, index):
    if not lines[index].strip().startswith(TTL_TAKEN):
        return index
    for i in range(index + 1, len(lines)):
        line = lines[i].strip()
        if TTL.fullmatch(line):
            return i
        if line.casefold().startswith(("; [mesh:", "ib =")) or DRAW.match(line):
            break
    return index


def emit(node, sections, tail, components, ns, assets):
    enabled = f"$ssmtdragtear_enabled_{ns}"
    active = f"$ssmtdragtear_active_{ns}"
    ready = f"$ssmtdragtear_ready_{ns}"
    epoch = f"$ssmtdragtear_epoch_{ns}"
    previous_x, previous_y = (f"$ssmtdragtear_prev_{axis}_{ns}" for axis in ("x", "y"))
    guard = f"if {enabled} == 1 && {ready} == 1"
    gesture_shader, mask_shader = [assets.put(name, (SHADER_DIR / name).read_bytes()) for name in (
        "rzm_tear_gesture.hlsl", "rzm_tear_mask.hlsl",
    )]
    sites = defaultdict(dict)
    wrappers = defaultdict(dict)
    clears, clear_masks, dispatch = [], [], []
    ttl_masks = {}
    section_names = {name[1:-1].casefold() for name in sections}
    for comp in components:
        if "tear_token" not in comp:
            continue
        cn = comp["comp_name"]
        for suffix in ["", *(f"P{i}" for i in range(len(comp["parts"])))]:
            detect = sections[f"[CustomShaderDragDetect{cn}{suffix}_{ns}]"]
            index = next(i for i, line in enumerate(detect) if line.strip().startswith("x28 =")) + 1
            detect[index:index] = [BEGIN, f"x28 = {comp['tear_token']}", END]
        current_parts = {part["ib_resource"]: part for part in node._collect_draw_parts(sections, comp["hash"])}
        for part_id, part in enumerate(comp["parts"]):
            if "tear" not in part:
                continue
            part_tag, gesture, step = _emit_part(sections, comp, part_id, ns, gesture_shader)
            clears.append(f"clear = {gesture}")
            dispatch.append(f"run = {step}")
            _, owners, _, atlases = part["tear"]
            masks = {}
            for owner, atlas in atlases.items():
                tag = part_tag.removesuffix(f"_{ns}") + f"M{owner}_{ns}"
                mask, paint = _emit_mask(sections, tag, owner, atlas, gesture, mask_shader)
                clear_masks.append(f"clear = {mask}")
                dispatch.append(f"run = {paint}")
                masks[owner] = (mask, tag)
            for record in current_parts.get(part["ib_resource"], {}).get("draw_records", ()):
                start, count = record["draw_offset"], record["draw_count"]
                if start % 3 or count % 3:
                    continue
                ownership = np.unique(owners[start // 3:(start + count) // 3])
                if len(ownership) != 1 or ownership[0] not in masks:
                    continue
                mask, tag = masks[ownership[0]]
                section, index = record["section"], record["line_index"]
                index = _live_draw_index(sections[section], index)
                line = sections[section][index].strip()
                ttl = TTL.fullmatch(line)
                run = RUN.fullmatch(line)
                if ttl:
                    ttl_masks[tag] = mask
                if ttl and ttl[1].casefold() != TTL_DRAW:
                    wrappers[ttl[1].casefold()][(part["ib_resource"], tag)] = (mask, tag)
                elif ttl or DRAW.match(line) or (run and run[1].casefold() in section_names):
                    sites[section][index] = (mask, tag if ttl else None)
    for section, locations in sites.items():
        _wrap(sections[section], locations, guard, ns)
    for section, lines in sections.items():
        if section[1:-1].casefold() in wrappers:
            header_and_lines = [section, *lines]
            _wrap_ttl(header_and_lines, wrappers, guard, ns)
            sections[section] = header_and_lines[1:]
    if wrappers and tail:
        tail_lines = tail.splitlines()
        _wrap_ttl(tail_lines, wrappers, guard, ns)
        tail = "\n".join(tail_lines) + "\n"

    sections[f"[ResourceDragTearSavedPS_{ns}]"] = []
    sections["[Constants]"].extend([
        BEGIN, f"global {enabled} = 1", f"global {active} = 0", f"global {ready} = 0", f"global {epoch} = 0",
        f"global {previous_x} = -1", f"global {previous_y} = -1", f"global $ssmtdragtear_saved_magic_{ns} = 0", END,
    ])
    _emit_pixel_regex(sections, ns)
    if ttl_masks:
        filename = "rzm_tear_ttl.hlsl"
        _emit_ttl(sections, ttl_masks, ns, epoch, assets.put(filename, (SHADER_DIR / filename).read_bytes()))
    sections[f"[KeyDragTear_{ns}]"] = [
        f"condition = $ssmtdrag_drawn_{ns} == 1", "key = ctrl alt Y", "type = cycle", f"{enabled} = 0, 1",
    ]
    mode = node._runtime_variable_names(ns)[0]
    update = f"CommandListDragTearUpdate_{ns}"
    save_slots, restore_slots = [], []
    for slot in range(106, 111):
        saved = f"ResourceDragTearSavedCsT{slot}_{ns}"
        sections[f"[{saved}]"] = []
        save_slots.append(f"\t{saved} = ref cs-t{slot}")
        restore_slots.extend((f"\tcs-t{slot} = ref {saved}", f"\t{saved} = null"))
    sections[f"[{update}]"] = [
        f"if {enabled} == 1 && {mode} >= 2 && $isMouseButtonDown == 1"
        f" && ({active} == 0 || {previous_x} != $cursorX || {previous_y} != $cursorY)",
        f"\tif {ready} == 0", *("\t\t" + line for line in clear_masks),
        f"\t\t{ready} = 1", "\tendif",
        f"\tif {active} == 0", *("\t\t" + line for line in clears),
        f"\t\t{active} = 1", "\tendif", *save_slots,
        "\tlocal $ssmtdragtear_saved_owner = y494",
        *("\t" + line for line in dispatch), *restore_slots,
        "\ty494 = $ssmtdragtear_saved_owner",
        f"\t{epoch} = {epoch} + 1", f"\t{previous_x} = $cursorX", f"\t{previous_y} = $cursorY", "endif",
    ]
    pin = sections[f"[CommandListDragPinDetected_{ns}]"]
    index = pin.index(f"\trun = CustomShaderDragUpdateScreenJiggle_{ns}") + 1
    pin[index:index] = ["\t" + BEGIN, f"\trun = {update}", "\t" + END]
    present = sections["[Present]"]
    index = next(i for i, line in enumerate(present) if line.strip() == "; --- DRAG PRESENT END ---")
    present[index:index] = [
        BEGIN, f"if {active} == 1 && ({enabled} == 0 || {mode} < 2 || $isMouseButtonDown == 0"
        f" || $inputMode != 0 || $ssmtdrag_mode_{ns} == 0 || $ssmtdrag_drawn_{ns} == 0)",
        f"\t{active} = 0", "endif", END,
    ]
    assets.finish()
    return tail


def prune(root, namespace, tokens):
    prune_assets(root, namespace, tokens, family=FAMILY)
