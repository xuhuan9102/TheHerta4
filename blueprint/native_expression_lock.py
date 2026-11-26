"""Per-channel overrides for the ZZMI native expression paths."""

import re

import numpy as np

from . import native_morph_driver as morph


WEIGHT_SHADER = "zzmi_native_expression_lock_weights.hlsl"
TRANSFER_SHADER = "zzmi_native_expression_transfer_locked.hlsl"
LOCK_WEIGHT_OFFSET = 16
WEIGHT_REGISTER_START = 100


def parse_channels(value):
    tokens = re.split(r"[,，\s]+", str(value or "").strip())
    if tokens == [""]:
        return ()
    if any(not token.isascii() or not token.isdecimal() or int(token) >= 127 for token in tokens):
        raise ValueError("原生表情锁定通道请填写 0–126 的编号，例如 2, 15。")
    return tuple(sorted({int(token) for token in tokens}))


def register_for_slot(slot):
    return f"{'xyzw'[slot % 4]}{WEIGHT_REGISTER_START + slot // 4}"


def assignment_lines(strength, slot):
    register = register_for_slot(slot)
    return [
        f"{register} = 0",
        f"if {strength} > 4",
        f"    {register} = {LOCK_WEIGHT_OFFSET + 4}",
        f"elif {strength} < -4",
        f"    {register} = {LOCK_WEIGHT_OFFSET - 4}",
        f"elif {strength} != 0",
        f"    {register} = {LOCK_WEIGHT_OFFSET} + {strength}",
        "endif",
    ]


def prepare_transfer_bridge(draw_hash, indices, keys, attributes, native_rows, reserved, source_filters=None):
    available = {key.shape_key_index for key in keys}
    indices = tuple(index for index in indices if index in available and f"Deform {index}" in attributes)
    if not indices:
        return None
    plans = morph.build_native_morph_plans(
        {draw_hash: keys}, {draw_hash: {index: slot for slot, index in enumerate(indices)}},
        reserved, source_filters,
    )
    deltas = np.zeros((len(indices), native_rows, 9), dtype="<f4")
    for slot, index in enumerate(indices):
        entry = attributes[f"Deform {index}"]
        if entry.vertex_ids.size and int(entry.vertex_ids.max()) >= native_rows:
            raise ValueError(f"ZZMI {draw_hash} 的 Deform {index} 超出原生表情顶点范围。")
        np.add.at(deltas[slot], entry.vertex_ids, entry.deltas)
    return indices, plans[draw_hash], deltas.tobytes()


def render_transfer_locks(sections, draw_hash, variables, delta_filename):
    """Install a correction dispatch ahead of the unmodified native vertex transfer."""
    stem = f"ZZMINativeExpression_{draw_hash}"
    transfer = f"CustomShader{stem}"
    weights = f"Resource{stem}_NativeLockWeights"
    correction = f"Resource{stem}_LockCorrection"
    delta = f"Resource{stem}_LockDelta"
    prepare = f"CustomShader{stem}_PrepareLocks"

    for name, lines in sections.items():
        if name.startswith("[TextureOverride"):
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() == f"run = {transfer}":
                    indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                    lines.insert(i, f"{indent}run = {prepare}")

    lines = sections[f"[{transfer}]"]
    lines[0] = f"cs = ./res/{TRANSFER_SHADER}"
    dispatch = next(i for i, line in enumerate(lines) if line.startswith("Dispatch ="))
    lines[dispatch:dispatch] = [f"cs-t109 = {correction}", f"cs-t110 = {delta}"]
    lines.extend(["cs-t109 = null", "cs-t110 = null"])
    typed_buffer = ["type = RWBuffer", "format = R32_FLOAT", f"array = {len(variables)}",
                    "bind_flags = shader_resource unordered_access"]
    sections[f"[{weights}]"] = typed_buffer.copy()
    sections[f"[{correction}]"] = typed_buffer.copy()
    sections[f"[{delta}]"] = ["type = StructuredBuffer", "stride = 12", f"filename = {delta_filename}"]
    prepare_lines = [f"cs = ./res/{WEIGHT_SHADER}"]
    for slot, strength in enumerate(variables):
        name = register_for_slot(slot)
        prepare_lines.append(f"local $ssmt_saved_lock_{name} = {name}")
        prepare_lines.extend(assignment_lines(strength, slot))
    prepare_lines.extend([
        f"cs-t0 = ref {weights}", f"cs-u0 = ref {correction}",
        f"Dispatch = {(len(variables) + 63) // 64}, 1, 1", "cs-u0 = null", "cs-t0 = null",
    ])
    for slot in range(len(variables)):
        name = register_for_slot(slot)
        prepare_lines.append(f"{name} = $ssmt_saved_lock_{name}")
    sections[f"[{prepare}]"] = prepare_lines
    return weights
