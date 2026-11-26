from types import SimpleNamespace

import numpy as np
import pytest

from blueprint import native_expression_lock as lock
from blueprint.native_morph_driver import NativeMorphKey


def test_channel_settings_use_zero_for_native_and_nonzero_for_manual_strength():
    channels = lock.parse_channels("2, 15")
    assert channels == (2, 15)
    assert lock.parse_channels("") == ()
    with pytest.raises(ValueError):
        lock.parse_channels("127")
    assert lock.assignment_lines("$Freq_Deform_15", 1) == [
        "y100 = 0",
        "if $Freq_Deform_15 > 4",
        "    y100 = 20",
        "elif $Freq_Deform_15 < -4",
        "    y100 = 12",
        "elif $Freq_Deform_15 != 0",
        "    y100 = 16 + $Freq_Deform_15",
        "endif",
    ]


def test_transfer_builds_lookup_and_deltas_for_only_the_selected_native_channels():
    keys = [NativeMorphKey("0123456789abcdef", "12345678", "87654321", 4, 2, i) for i in (2, 9, 15)]
    attributes = {
        f"Deform {i}": SimpleNamespace(vertex_ids=np.array([1, 1]), deltas=np.full((2, 9), i, np.float32))
        for i in (2, 9, 15)
    }
    indices, plan, payload = lock.prepare_transfer_bridge("8b067f99", (2, 15), keys, attributes, 3, set())
    assert indices == (2, 15)
    assert [entry.shape_key_slot for entry in plan.entries] == [0, 1]
    rows = np.frombuffer(payload, dtype="<f4").reshape(2, 3, 9)
    np.testing.assert_array_equal(rows[:, 1], np.array([[4] * 9, [30] * 9]))
    assert np.count_nonzero(rows[:, [0, 2]]) == 0


def test_lock_correction_runs_before_vertex_transfer():
    shader = "CustomShaderZZMINativeExpression_8b067f99"
    hook = "[TextureOverride_VB_8b067f99_face_Blend]"
    sections = {hook: [f"run = {shader}"], f"[{shader}]": ["cs = ./res/old.hlsl", "Dispatch = 55, 1, 1"]}

    lock.render_transfer_locks(sections, "8b067f99", ("$Freq_Deform_2", "$CustomBlink"), "Meshes0000/a.buf")

    assert sections[hook] == [f"run = {shader}_PrepareLocks", f"run = {shader}"]
    assert sections[f"[{shader}]"][1:3] == [
        "cs-t109 = ResourceZZMINativeExpression_8b067f99_LockCorrection",
        "cs-t110 = ResourceZZMINativeExpression_8b067f99_LockDelta",
    ]
    assert "    y100 = 16 + $CustomBlink" in sections[f"[{shader}_PrepareLocks]"]
