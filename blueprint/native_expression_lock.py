"""Per-channel overrides for the ZZMI native expression paths."""

import numpy as np

from . import native_morph_driver as morph


WEIGHT_SHADER = "zzmi_native_expression_lock_weights.hlsl"
TRANSFER_SHADER = "zzmi_native_expression_transfer_locked.hlsl"
WEIGHT_REGISTER_START = 100
DELTA_DTYPE = np.dtype([("slot", "<u4"), ("delta", "<f4", (9,))])


def transfer_gate(draw_hash):
    return f"$ssmt_native_expression_override_{morph.safe_name(draw_hash)}"


def nonzero_condition(variables):
    return " || ".join(f"{name} != 0" for name in dict.fromkeys(variables))


def register_for_slot(slot):
    return f"{'xyzw'[slot % 4]}{WEIGHT_REGISTER_START + slot // 4}"


def prepare_transfer_bridge(draw_hash, keys, attributes, native_rows, reserved, source_filters=None):
    entries = {
        key.shape_key_index: attributes[f"Deform {key.shape_key_index}"]
        for key in keys if f"Deform {key.shape_key_index}" in attributes
    }
    indices = tuple(sorted(index for index, entry in entries.items() if entry.vertex_ids.size))
    if not indices:
        return None
    plans = morph.build_native_morph_plans(
        {draw_hash: keys}, {draw_hash: {index: slot for slot, index in enumerate(indices)}},
        reserved, source_filters,
    )
    vertices = []
    records = []
    for slot, index in enumerate(indices):
        entry = entries[index]
        vertex_ids, inverse = np.unique(entry.vertex_ids, return_inverse=True)
        if int(vertex_ids[0]) < 0 or int(vertex_ids[-1]) >= native_rows:
            raise ValueError(f"ZZMI {draw_hash} 的 Deform {index} 超出原生表情顶点范围。")
        deltas = np.zeros(len(vertex_ids), dtype=DELTA_DTYPE)
        deltas["slot"] = slot
        np.add.at(deltas["delta"], inverse, entry.deltas)
        vertices.append(vertex_ids)
        records.append(deltas)

    vertex_ids = np.concatenate(vertices).astype(np.intp, copy=False)
    order = np.argsort(vertex_ids, kind="stable")
    offsets = np.zeros(native_rows + 1, dtype="<u4")
    np.cumsum(np.bincount(vertex_ids, minlength=native_rows), out=offsets[1:])
    return indices, plans[draw_hash], np.concatenate(records)[order].tobytes(), offsets.tobytes()


def render_transfer_locks(sections, draw_hash, variables, delta_filename, offsets_filename):
    """Switch between the native transfer and an optional correction for manual weights."""
    stem = f"ZZMINativeExpression_{draw_hash}"
    transfer = f"CustomShader{stem}"
    weights = f"Resource{stem}_NativeLockWeights"
    correction = f"Resource{stem}_LockCorrection"
    delta = f"Resource{stem}_LockDelta"
    offsets = f"Resource{stem}_LockOffsets"
    prepare = f"CustomShader{stem}_PrepareLocks"
    locked_transfer = f"{transfer}_Locked"
    sections[f"[CommandList{stem}]"] = [
        f"if {transfer_gate(draw_hash)} != 0",
        f"    run = {prepare}",
        f"    run = {locked_transfer}",
        "else",
        f"    run = {transfer}",
        "endif",
    ]
    lines = sections[f"[{transfer}]"].copy()
    lines[0] = f"cs = ./res/{TRANSFER_SHADER}"
    dispatch = next(i for i, line in enumerate(lines) if line.startswith("Dispatch ="))
    lines[dispatch:dispatch] = [f"cs-t109 = {correction}", f"cs-t110 = {delta}", f"cs-t111 = {offsets}"]
    lines.extend(["cs-t109 = null", "cs-t110 = null", "cs-t111 = null"])
    sections[f"[{locked_transfer}]"] = lines
    typed_buffer = ["type = RWBuffer", "format = R32_FLOAT", f"array = {len(variables)}",
                    "bind_flags = shader_resource unordered_access"]
    sections[f"[{weights}]"] = typed_buffer.copy()
    sections[f"[{correction}]"] = typed_buffer.copy()
    sections[f"[{delta}]"] = ["type = StructuredBuffer", f"stride = {DELTA_DTYPE.itemsize}", f"filename = {delta_filename}"]
    sections[f"[{offsets}]"] = ["type = StructuredBuffer", "stride = 4", f"filename = {offsets_filename}"]
    prepare_lines = [f"cs = ./res/{WEIGHT_SHADER}"]
    for slot, strength in enumerate(variables):
        name = register_for_slot(slot)
        prepare_lines.append(f"local $ssmt_saved_lock_{name} = {name}")
        prepare_lines.append(f"{name} = {strength}")
    prepare_lines.extend([
        f"cs-t0 = ref {weights}", f"cs-u0 = ref {correction}",
        f"Dispatch = {(len(variables) + 63) // 64}, 1, 1", "cs-u0 = null", "cs-t0 = null",
    ])
    for slot in range(len(variables)):
        name = register_for_slot(slot)
        prepare_lines.append(f"{name} = $ssmt_saved_lock_{name}")
    sections[f"[{prepare}]"] = prepare_lines
    return weights
