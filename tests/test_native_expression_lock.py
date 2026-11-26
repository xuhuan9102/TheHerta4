from types import SimpleNamespace

import numpy as np
from blueprint import native_expression_lock as lock
from blueprint.native_morph_driver import NativeMorphKey


def test_transfer_builds_sparse_deltas_for_all_mapped_native_channels():
    keys = [NativeMorphKey("0123456789abcdef", "12345678", "87654321", 4, 2, i) for i in (15, 9, 2, 19)]
    attributes = {
        f"Deform {index}": SimpleNamespace(vertex_ids=np.array(ids), deltas=np.full((2, 9), index, np.float32))
        for index, ids in ((2, [1, 1]), (9, [1, 2]), (15, [2, 2]), (21, [0, 0]))
    }
    indices, plan, payload, offsets = lock.prepare_transfer_bridge("8b067f99", keys, attributes, 4, set())
    assert indices == (2, 9, 15)
    assert [entry.shape_key_slot for entry in plan.entries] == [0, 1, 2]
    np.testing.assert_array_equal(np.frombuffer(offsets, dtype="<u4"), [0, 0, 2, 4, 4])
    rows = np.frombuffer(payload, dtype=lock.DELTA_DTYPE)
    np.testing.assert_array_equal(rows["slot"], [0, 1, 1, 2])
    np.testing.assert_array_equal(rows["delta"], np.repeat([[4], [9], [9], [30]], 9, axis=1))


def test_transfer_dispatches_correction_only_when_manual_control_is_active():
    shader = "CustomShaderZZMINativeExpression_8b067f99"
    base = ["cs = ./res/old.hlsl", "Dispatch = 55, 1, 1"]
    sections = {f"[{shader}]": base.copy()}

    lock.render_transfer_locks(sections, "8b067f99", ("$Freq_Deform_2", "$CustomBlink"),
                               "Meshes0000/deltas.buf", "Meshes0000/offsets.buf")

    assert sections["[CommandListZZMINativeExpression_8b067f99]"] == [
        "if $ssmt_native_expression_override_8b067f99 != 0",
        f"    run = {shader}_PrepareLocks", f"    run = {shader}_Locked",
        "else", f"    run = {shader}", "endif",
    ]
    assert sections[f"[{shader}]"] == base
    assert sections[f"[{shader}_Locked]"][1:4] == [
        "cs-t109 = ResourceZZMINativeExpression_8b067f99_LockCorrection",
        "cs-t110 = ResourceZZMINativeExpression_8b067f99_LockDelta",
        "cs-t111 = ResourceZZMINativeExpression_8b067f99_LockOffsets",
    ]
    assert "y100 = $CustomBlink" in sections[f"[{shader}_PrepareLocks]"]
