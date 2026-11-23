"""Optional render-only holes for the existing ZZMI drag interaction."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from .drag_cloth import _Assets, _field, prune_assets

FAMILY = "drag_tear"
SHADER_DIR = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction"
BEGIN = "; --- SSMT TEAR BEGIN ---"
END = "; --- SSMT TEAR END ---"
DRAW = re.compile(r"drawindexed\s*=", re.IGNORECASE)
RUN = re.compile(r"run\s*=\s*(\S+)$", re.IGNORECASE)
TTL = re.compile(r"run\s*=\s*(CommandList\\TTL\\Draw|CommandListSSMTTTLDraw_\S+)\s*$", re.IGNORECASE)
TTL_DRAW = r"commandlist\ttl\draw"
TTL_TAKEN = "; [SSMT TTL 已接管]"


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
    prefixes = ("ResourceDragTear", "CustomShaderDragTear", "CommandListDragTear", "KeyDragTear")
    for name, lines in list(sections.items()):
        if name[1:].startswith(prefixes) and name.endswith(f"_{ns}]"):
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
    return (filename, owners) if owners.any() else None


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
            if assets is None:
                assets = _Assets(root, token, ns, family=FAMILY)
            filename, owners = data
            header = np.array([component_id, part_id], dtype="<u4")
            part["tear"] = (filename, owners, assets.put(
                f"{cn}-p{part_id}-owners.buf", np.concatenate((header, owners)),
            ))
            comp["tear_token"] = component_id
        if "tear_token" in comp:
            comp["tear_rest"] = assets.put(f"{cn}-rest.buf", positions.astype("<f4", copy=False))
    return assets


def _emit_part(sections, comp, part_id, assets, ns, shaders):
    part = comp["parts"][part_id]
    original_file, owners, owners_file = part["tear"]
    tag = f"{comp['comp_name']}P{part_id}_{ns}"
    resource = "ResourceDragTear"
    rest = f"{resource}Rest_{comp['comp_name']}_{ns}"
    original, display, faces, gesture, saved = (
        f"{resource}{name}_{tag}" for name in ("Original", "Display", "Faces", "Gesture", "SavedIB")
    )
    base = ["type = Buffer", "format = R32_UINT", f"filename = {original_file}"]
    sections[f"[{rest}]"] = ["type = StructuredBuffer", "stride = 12", f"filename = {comp['tear_rest']}"]
    sections[f"[{original}]"] = base
    sections[f"[{display}]"] = base + ["bind_flags = index_buffer unordered_access", "misc_flags = none"]
    sections[f"[{faces}]"] = ["type = Buffer", "format = R32_UINT", f"filename = {owners_file}"]
    sections[f"[{gesture}]"] = ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 4"]
    sections[f"[{saved}]"] = []
    shared = [f"cs-t108 = {rest}", f"cs-t109 = {original}", f"cs-t110 = {faces}"]
    step = f"CustomShaderDragTearStep_{tag}"
    cut = f"CustomShaderDragTearCut_{tag}"
    sections[f"[{step}]"] = [
        f"cs = {shaders[0]}", f"cs-t106 = ResourceDragPinnedDetectInfo_{ns}",
        f"cs-t107 = ResourceDragJiggleScreenState_{ns}", *shared,
        f"cs-u0 = {gesture}", "dispatch = 1, 1, 1", "cs-u0 = null", "cs-t107 = null",
    ]
    sections[f"[{cut}]"] = [
        f"cs = {shaders[1]}", f"cs-t106 = {gesture}", *shared,
        f"cs-u0 = {display}", f"dispatch = {(len(owners) + 63) // 64}, 1, 1",
        "cs-u0 = null", "cs-t106 = null", "cs-t108 = null", "cs-t109 = null", "cs-t110 = null",
    ]
    return display, saved, gesture, step, cut


def _wrap(lines, sites, enabled):
    for index, (display, saved) in sorted(sites.items(), reverse=True):
        indent = re.match(r"[ \t]*", lines[index]).group()
        before = [BEGIN, f"if {enabled} == 1", f"\t{saved} = ref ib",
                  f"\tib = ref {display} unless_null", "endif", END]
        after = [BEGIN, f"if {enabled} == 1", f"\tib = ref {saved}",
                 f"\t{saved} = null", "endif", END]
        lines[index + 1:index + 1] = [indent + line for line in after]
        lines[index:index] = [indent + line for line in before]


def _wrap_ttl(lines, wrappers, enabled):
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
    _wrap(lines, sites, enabled)


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
    shaders = [assets.put(name, (SHADER_DIR / name).read_bytes()) for name in (
        "rzm_tear_gesture.hlsl", "rzm_tear_indices.hlsl",
    )]
    sites = defaultdict(dict)
    wrappers = defaultdict(dict)
    clears, dispatch = [], []
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
            display, saved, gesture, step, cut = _emit_part(sections, comp, part_id, assets, ns, shaders)
            clears.append(f"clear = {gesture}")
            dispatch.extend((f"run = {step}", f"run = {cut}"))
            owners = part["tear"][1]
            for record in current_parts.get(part["ib_resource"], {}).get("draw_records", ()):
                start, count = record["draw_offset"], record["draw_count"]
                if start % 3 or count % 3 or not owners[start // 3:(start + count) // 3].any():
                    continue
                section, index = record["section"], record["line_index"]
                index = _live_draw_index(sections[section], index)
                line = sections[section][index].strip()
                ttl = TTL.fullmatch(line)
                run = RUN.fullmatch(line)
                if ttl and ttl[1].casefold() != TTL_DRAW:
                    wrappers[ttl[1].casefold()][part["ib_resource"]] = (display, saved)
                elif ttl or DRAW.match(line) or (run and run[1].casefold() in section_names):
                    sites[section][index] = (display, saved)
    for section, locations in sites.items():
        _wrap(sections[section], locations, enabled)
    for section, lines in sections.items():
        if section[1:-1].casefold() in wrappers:
            header_and_lines = [section, *lines]
            _wrap_ttl(header_and_lines, wrappers, enabled)
            sections[section] = header_and_lines[1:]
    if wrappers and tail:
        tail_lines = tail.splitlines()
        _wrap_ttl(tail_lines, wrappers, enabled)
        tail = "\n".join(tail_lines) + "\n"

    sections["[Constants]"].extend([BEGIN, f"global {enabled} = 1", f"global {active} = 0", END])
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
        f"if {enabled} == 1 && {mode} >= 2 && $isMouseButtonDown == 1",
        f"\tif {active} == 0", *("\t\t" + line for line in clears),
        f"\t\t{active} = 1", "\tendif", *save_slots,
        *("\t" + line for line in dispatch), *restore_slots, "endif",
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
