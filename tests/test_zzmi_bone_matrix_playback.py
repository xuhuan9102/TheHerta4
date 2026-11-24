import importlib
import json
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest
import numpy as np

package = ModuleType("_zzmi_bone_playback_test")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
playback = importlib.import_module(package.__name__ + ".common.zzmi_bone_matrix_playback")
layered = importlib.import_module(package.__name__ + ".common.zzmi_bone_matrix_layers")

PALETTE = "ResourceZZMergedSkeleton_G0_s1"
INI = f"""[Constants]
global $zz_ms_seen_01 = 0
[Present]
$zz_ms_seen_01 = 0

[TextureOverride_Deform]
$zz_ms_occ_0 = 1
if $zz_ms_occ_0 == 1
    ResourceZZPalette_95de0d39_s1 = copy vs-t0 unless_null
endif
vs-t0 = {PALETTE}

[TextureOverride_Render]
hash = 95de0d39
match_first_index = 48399
match_index_count = 15021
ib = ResourceTriangles
drawindexed = 3, 0, 0

[TextureOverride_OtherCostume]
hash = e35322ac
match_first_index = 48399
match_index_count = 15021
ib = ResourceOtherTriangles
drawindexed = 3, 0, 0

[{PALETTE}]
array = 15
[ResourceSSMTBoneMatrixBindPosition_95de0d39]
type = StructuredBuffer
array = 3
[ResourceSSMTBoneMatrixBindBlend_95de0d39]
type = StructuredBuffer
array = 3
"""


def clip():
    return playback.BoneMatrixClip((0, 2), 0, 5, 26, bytes(26 * 2 * 48))


def test_sparse_clip_is_private_to_the_exact_render_ib(tmp_path):
    ini = tmp_path / "test.ini"
    ini.write_text(INI, encoding="utf-8")
    playback.export_clip(ini, "$switch", "$pose_frame", clip())
    result = ini.read_text(encoding="utf-8")

    deform = result.split("[TextureOverride_Deform]", 1)[1].split("[TextureOverride_Render]", 1)[0]
    assert "ResourceSSMTBoneMatrixRender0SO = ref so0" in deform
    assert f"vs-t0 = {PALETTE}" in deform
    assert "run = CustomShaderSSMT" not in deform
    assert "vs-t0 = ref ResourceSSMT" not in result

    render = result.split("[TextureOverride_Render]", 1)[1].split("[TextureOverride_OtherCostume]", 1)[0]
    assert "if $switch != 0 && @vb0 != 0" in render
    assert "$zz_ms_seen_01 > 0 && @vb0 === @ResourceSSMTBoneMatrixRender0SO" in render
    assert "vb0 = ref ResourceSSMTBoneMatrixRender0Output" in render
    assert len([line for line in render.splitlines() if line.strip().startswith("drawindexed =")]) == 1
    assert "handling = skip" in render
    assert "vb0 = ref ResourceSSMTBoneMatrixRenderOriginalVB" in render

    other = result.split("[TextureOverride_OtherCostume]", 1)[1].split(f"[{PALETTE}]", 1)[0]
    assert "ResourceSSMTBoneMatrix" not in other
    assert "vs-cb1 =" not in result
    assert "x41 = $pose_frame - (5)" in result
    assets = tmp_path / "res"
    invalid = 0xFFFFFFFF
    assert struct.unpack("<5I", (assets / "SSMTBoneMatrixSlots.buf").read_bytes()) == (
        0, invalid, 1, invalid, invalid,
    )
    assert (assets / "SSMTBoneMatrixFrames.buf").read_bytes() == clip().matrices
    playback.export_clip(ini, "$switch", "$pose_frame", clip())
    assert ini.read_text(encoding="utf-8") == result


def test_playback_defaults_share_names_with_an_animation_driver():
    content = playback._configuration_text(INI, "$switch", "$pose_frame", clip())[0]
    driver_block = "[Constants]\nglobal persist $pose_frame = 1\n"
    combined = playback.reconcile_bone_matrix_defaults(driver_block + content)
    assert "\n; SSMT bone matrix default\n; global $pose_frame = 5" in combined
    assert "\n; SSMT bone matrix default\nglobal $switch = 0" in combined
    assert playback.reconcile_bone_matrix_defaults(combined.removeprefix(driver_block)) == content
    legacy = "global $pose_frame = 5 ; SSMT bone matrix default\n"
    assert playback.reconcile_bone_matrix_defaults(legacy) == "; SSMT bone matrix default\nglobal $pose_frame = 5\n"


def test_playback_can_start_enabled_at_frame_zero():
    text, _ = playback._configuration_text(
        INI, "$switch", "$pose_frame", clip(), play_on_load=True, initial_frame=0,
    )
    assert "; SSMT bone matrix default\nglobal $switch = 1" in text
    assert "; SSMT bone matrix default\nglobal $pose_frame = 0" in text


def test_unscoped_render_override_is_rejected():
    with pytest.raises(ValueError, match="按 IB、起始索引和索引数隔离"):
        playback._configuration_text(INI.replace("match_index_count = 15021\n", ""), "$switch", "$pose_frame", clip())


def test_full_calibration_reads_current_cb1_without_writing_it(tmp_path):
    ini = tmp_path / "character.ini"
    ini.write_text(INI, encoding="utf-8")
    full = playback.BoneMatrixClip((0, 1, 2, 3, 4), 0, 29, 2, bytes(480), (0.0, 0.02, 0.95))
    playback.export_clip(ini, "$switch", "$pose_frame", full)
    result = ini.read_text(encoding="utf-8")
    assert "ResourceSSMTBoneMatrixRender0ObjectCB = copy vs-cb1" in result
    assert "cs-u7 = ref ResourceSSMTBoneMatrixRender0World" in result
    assert "cs-t96 = ref ResourceSSMTBoneMatrixRender0ObjectCB" in result
    assert "cs-t97 = ref ResourceSSMTBoneMatrixRender0World" in result
    assert "vs-cb1 =" not in result
    assert "CommandListSSMTBoneMatrixSkin0" in result
    playback.export_clip(ini, "$switch", "$pose_frame", full)
    assert ini.read_text(encoding="utf-8") == result

    playback.export_clip(ini, "$switch", "$pose_frame", clip())
    result = ini.read_text(encoding="utf-8")
    assert "CustomShaderSSMTBoneMatrixCalibrate" not in result
    assert "vs-cb1 =" not in result


def test_full_clip_exports_weighted_ground_support(tmp_path):
    ini = tmp_path / "character.ini"
    ini.write_text(INI.replace(
        "[ResourceSSMTBoneMatrixBindPosition_95de0d39]",
        "[ResourceSSMTBoneMatrixBindPosition_95de0d39]\nfilename = position.buf",
    ).replace(
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]",
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]\nfilename = blend.buf",
    ), encoding="utf-8")
    (tmp_path / "position.buf").write_bytes(b"".join(
        struct.pack("<10f", x, 0, z, *([0] * 7))
        for x, z in ((-0.1, 0), (0.1, 0.005), (0, 1.5))
    ))
    (tmp_path / "blend.buf").write_bytes(struct.pack("<4f4I", 1, 0, 0, 0, 2, 0, 0, 0) * 3)
    full = playback.BoneMatrixClip((0, 1, 2, 3, 4), 2, 0, 1, bytes(240), (0, 0, 0.95))
    playback.export_clip(ini, "$switch", "$pose_frame", full)
    rows = list(struct.iter_unpack("<8f4I", (tmp_path / "res/SSMTBoneMatrixGroundSupport.buf").read_bytes()))
    assert len(rows) == 2
    assert all(row[4:8] == (1, 0, 0, 0) and row[8] == 2 for row in rows)
    assert "cs-t94 = ref ResourceSSMTBoneMatrixRenderSupport" in ini.read_text(encoding="utf-8")


def test_sparse_chain_attaches_to_nearest_unreplaced_parent(tmp_path):
    # 3 and 4 are authored. Their shared first original ancestor is 2.
    selected = (3, 4)
    parents = playback.attachment_slots(selected, {3: 2, 4: 3, 2: 0}, 0)
    assert parents == (2, 2)
    ini = tmp_path / "arm.ini"
    ini.write_text(INI, encoding="utf-8")
    partial = playback.BoneMatrixClip(selected, 0, 0, 1, bytes(96), parents=parents)
    playback.export_clip(ini, "$switch", "$pose_frame", partial)
    assert struct.unpack("<2I", (tmp_path / "res/SSMTBoneMatrixParents.buf").read_bytes()) == parents
    assert "cs-t98 = ref ResourceSSMTBoneMatrixParents" in ini.read_text(encoding="utf-8")


def test_expression_input_and_exported_uv_follow_the_private_draw():
    source = INI.replace(
        "[TextureOverride_Deform]\n",
        "[TextureOverride_Deform]\nrun = CustomShaderZZMINativeExpression_95de0d39\n",
    ) + "\n[Resource95de0d39Texcoord]\n"
    text, _ = playback._configuration_text(source, "$switch", "$pose_frame", clip())
    assert "ResourceSSMTBoneMatrixRender0MorphedPosition = copy vb0" in text
    assert "cs-t90 = ref ResourceSSMTBoneMatrixRender0MorphedPosition" in text
    assert "vb1 = ref Resource95de0d39Texcoord" in text
    assert "vb1 = ref ResourceSSMTBoneMatrixRenderOriginalUV" in text


def test_snapshot_key_and_variable_wrap_only_the_matching_draw(tmp_path):
    ini = tmp_path / "model.ini"
    ini.write_text(INI, encoding="utf-8")
    settings = playback.snapshot_settings("pose_freeze", "Ctrl+Alt+B", "$switch", "$pose_frame")
    playback.export_clip(ini, "$switch", "$pose_frame", clip(), cb1=settings)
    result = ini.read_text(encoding="utf-8")
    assert "global $pose_freeze = 0" in result
    assert "[KeySSMTBoneMatrixCB1Snapshot]\nkey = Ctrl Alt B\ntype = cycle\n$pose_freeze = 0,1" in result
    render = result.split("[TextureOverride_Render]", 1)[1].split("[TextureOverride_OtherCostume]", 1)[0]
    assert "if ($switch != 0 || $pose_freeze != 0) && @vb0 != 0" in render
    assert render.index("@vb0 === @ResourceSSMTBoneMatrixRender0SO") < render.index("run = CommandListSSMTBoneMatrixCB1")
    assert render.index("drawindexed = 3, 0, 0\nif") < render.index("vs-cb1 = ref ResourceSSMTBoneMatrixOriginalCB1")
    assert "copy vs-cb1" in result and "vs-cb1->offset" in result
    other = result.split("[TextureOverride_OtherCostume]", 1)[1].split(f"[{PALETTE}]", 1)[0]
    assert "CB1" not in other
    playback.export_clip(ini, "$switch", "$pose_frame", clip(), cb1=settings)
    assert ini.read_text(encoding="utf-8") == result
    playback.export_clip(ini, "$switch", "$pose_frame", clip())
    assert "CB1Snapshot" not in ini.read_text(encoding="utf-8")


def test_snapshot_key_allows_variable_only_control_and_rejects_ini_injection():
    assert playback.snapshot_settings("$freeze", "", "$switch", "$frame").key == ""
    with pytest.raises(ValueError, match="不同"):
        playback.snapshot_settings("Switch", "Ctrl+B", "$switch", "$frame")
    with pytest.raises(ValueError, match="单行"):
        playback.snapshot_settings("$freeze", "B\n[Present]", "$switch", "$frame")


def test_snapshot_only_export_uses_so_association_without_skin_assets(tmp_path):
    ini = tmp_path / "model.ini"
    init = INI.split("[ResourceSSMTBoneMatrixBindPosition_95de0d39]")[0]
    ini.write_text(init, encoding="utf-8")
    settings = playback.snapshot_settings("pose_freeze", "B")
    playback.export_cb1_snapshot(ini, settings)
    result = ini.read_text(encoding="utf-8")
    assert "ResourceSSMTBoneMatrixRender0SO = ref so0" in result
    assert "@vb0 === @ResourceSSMTBoneMatrixRender0SO" in result
    assert "global $pose_freeze = 0" in result
    assert "vs-cb1 = ref ResourceSSMTBoneMatrixOriginalCB1" in result
    assert "CommandListSSMTBoneMatrixSkin" not in result and "SSMTBoneMatrixFrames.buf" not in result
    assert not (tmp_path / "res").exists()
    other = result.split("[TextureOverride_OtherCostume]", 1)[1].split(f"[{PALETTE}]", 1)[0]
    assert "CB1" not in other
    playback.export_cb1_snapshot(ini, settings)
    assert ini.read_text(encoding="utf-8") == result


def test_snapshot_only_export_scopes_unmerged_draws_by_vertex_buffer(tmp_path):
    ini = tmp_path / "unmerged.ini"
    ini.write_text(
        "[TextureOverride_ForFreeze]\nhash = 95de0d39\nmatch_first_index = 0\n"
        "match_index_count = 3\nib = ResourceTriangles\ndrawindexed = 3, 0, 0\n"
        "[TextureOverride_Unscoped]\nhash = 95de0d39\ndrawindexed = 9, 0, 0\n",
        encoding="utf-8",
    )
    playback.export_cb1_snapshot(ini, playback.snapshot_settings("pose_freeze", "B"))
    result = ini.read_text(encoding="utf-8")
    assert "$ssmt_bm_cb1_vb_0_0_1 = @vb0" in result
    assert "$ssmt_bm_cb1_vb_0_0_1 === @vb0" in result
    assert "Render0SO" not in result
    end = result.split("[TextureOverride_Unscoped]", 1)[1].split("[Present]", 1)[0]
    assert end.lstrip().startswith("hash = 95de0d39\ndrawindexed = 9, 0, 0")


def _translated_clip(slots, moves):
    frames = np.repeat(np.eye(4, dtype="<f4")[None, :3], len(slots), axis=0)
    frames[:, :, 3] = moves
    return playback.BoneMatrixClip(tuple(slots), 0, 0, 1, frames.tobytes())


def test_multiple_rigs_and_separate_chains_share_global_parent_slots():
    left = _translated_clip([3, 4, 5], [(1, 0, 0), (1, 2, 0), (0, 0, 1)])
    right = _translated_clip([9], [(0, 3, 0)])
    # Chain 4's authored parent was 3. It now follows 9 from the other rig.
    # Unrelated chain 5 keeps its independently configured native shoulder.
    merged = playback.merge_pose_clips(
        [left, right], {3: 21, 4: 9, 5: 23, 9: 27}, {4: 3}, (21, 23, 27),
    )
    poses = np.frombuffer(merged.matrices, "<f4").reshape(4, 3, 4)
    assert merged.slots == (3, 4, 5, 9)
    assert merged.parents == (21, 27, 23, 27)
    np.testing.assert_array_equal(poses[:, :, 3], ((1, 0, 0), (0, 5, 0), (0, 0, 1), (0, 3, 0)))


def test_multi_rig_rejects_duplicate_slots_and_manual_parent_cycles():
    clip_1 = _translated_clip([3], [(0, 0, 0)])
    clip_2 = _translated_clip([4], [(0, 0, 0)])
    with pytest.raises(ValueError, match="同一个全局"):
        playback.merge_pose_clips([clip_1, clip_1], {}, {})
    with pytest.raises(ValueError, match="循环"):
        playback.merge_pose_clips([clip_1, clip_2], {3: 4, 4: 3}, {})


def _rotation(degrees):
    angle = np.deg2rad(degrees)
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0]], dtype="<f4")


def test_layer_priority_and_parent_are_exported_per_candidate(tmp_path):
    # In Blender A's 0 already contains its parent's 20 degrees. The packed
    # local delta must be 20 degrees, so a winning B track replaces only that delta.
    clip_a = playback.BoneMatrixClip((0, 1), 1, 0, 1, np.stack([_rotation(40), _rotation(20)]).tobytes())
    clip_b = playback.BoneMatrixClip((0,), 1, 0, 1, _rotation(-20).tobytes())
    a = layered.BoneMatrixLayer(clip_a, {0: 1}, {0: 1}, "$play_a", "$frame_a", True, 0, "K")
    b = layered.BoneMatrixLayer(clip_b, {0: 1}, {}, "$play_b", "$frame_b", False, 0)
    packed = dict(layered.pack_layers((a, b), 5, clip_a).assets)
    assert np.frombuffer(packed["SSMTBoneMatrixLayerCandidates.buf"], "<u4").tolist() == [2, 0, 1]
    tracks = np.frombuffer(packed["SSMTBoneMatrixLayerTracks.buf"], "<u4").reshape(-1, 2)
    assert tracks[[0, 2], 1].tolist() == [1, 1]
    sample = np.frombuffer(packed["SSMTBoneMatrixFrames.buf"], "<f4").reshape(-1, 3, 4)
    np.testing.assert_allclose(sample[0], _rotation(20), atol=1e-6)
    np.testing.assert_allclose(sample[2], _rotation(-20), atol=1e-6)
    np.testing.assert_allclose(sample[1, :, :3] @ sample[2, :, :3], np.eye(3), atol=1e-6)
    assert np.frombuffer(packed["SSMTBoneMatrixLayerControl.buf"], "<f4").size == 8

    ini = tmp_path / "model.ini"
    ini.write_text(INI, encoding="utf-8")
    playback.export_clip(ini, None, None, clip_a, rig_layers=(a, b))
    result = ini.read_text(encoding="utf-8")
    assert "global $play_a = 1" in result and "global $play_b = 0" in result
    assert "z43 = $frame_a - (0)" in result
    assert "z43 = $frame_b - (0)" in result
    assert "[KeySSMTBoneMatrixLayer0]\nkey = K\ntype = cycle\n$play_a = 0,1" in result
    assert "CommandListSSMTBoneMatrixUpdateLayers" in result
    playback.export_clip(ini, None, None, clip_a, rig_layers=(a, b))
    assert ini.read_text(encoding="utf-8") == result


def test_extended_slots_expand_only_private_palette_and_keep_native_fallback(tmp_path):
    asset = INI.replace(
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]",
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]\nfilename = Meshes/private.buf",
    )
    layout = {"stride": 32, "elements": [
        {"semantic": "BLENDWEIGHT", "index": 0, "format": "R32G32B32A32_FLOAT", "byte_width": 16, "offset": 0},
        {"semantic": "BLENDINDICES", "index": 0, "format": "R32G32B32A32_UINT", "byte_width": 16, "offset": 16},
    ]}
    asset = asset.replace("filename = Meshes/private.buf", "filename = Meshes/private.buf\n"
                          "; SSMT private blend layout " + json.dumps(layout))
    asset += "\n[Resource95de0d39Blend]\nfilename = Meshes/native.buf\n"
    asset += "\n[ResourceZZSkinBlend_95de0d39]\nfilename = ./Meshes/native.buf\n"
    (tmp_path / "Meshes").mkdir()
    data = struct.pack("<4f4I", 0.25, 0.75, 0.0, 0.0, 4, 6, 0, 0) * 3
    for fname in ("native", "private"):
        (tmp_path / "Meshes" / f"{fname}.buf").write_bytes(data)
    ini = tmp_path / "extended.ini"
    ini.write_text(asset, encoding="utf-8")
    extended = _translated_clip([5, 6], [(0, 0, 1), (0, 1, 0)])
    layer = layered.BoneMatrixLayer(extended, {5: 3, 6: 5}, {}, "$active", "$frame", False, 0)
    playback.export_clip(ini, None, None, extended, rig_layers=(layer,))
    written = ini.read_text(encoding="utf-8")
    native = written.split(f"[{PALETTE}]", 1)[1].split("[", 1)[0]
    resolved = written.split("[ResourceSSMTBoneMatrixRender0ResolvedPalette]", 1)[1].split("[", 1)[0]
    assert "array = 15" in native and "array = 21" in resolved
    assert "data = 2 1 0 5" in written and "cs-t104 = ref ResourceSSMTBoneMatrixLayerFallback" in written
    assert written.count("\nfilename = res/SSMTBoneMatrixNativeBlend_95de0d39.buf\n") == 2
    for part in ("native", "private"):
        assert (tmp_path / "Meshes" / f"{part}.buf").read_bytes() == data
    native_blend = (tmp_path / "res/SSMTBoneMatrixNativeBlend_95de0d39.buf").read_bytes()
    assert native_blend == struct.pack("<4f4I", 0.25, 0.75, 0.0, 0.0, 4, 3, 0, 0) * 3
    np.testing.assert_array_equal(
        np.fromfile(tmp_path / "res/SSMTBoneMatrixLayerFallback.buf", "<u4"), [3, 3],
    )
    playback.export_clip(ini, None, None, extended, rig_layers=(layer,))
    assert ini.read_text(encoding="utf-8") == written

    smaller_pool = tmp_path / "smaller-pool.ini"
    smaller_pool.write_text(
        asset.replace("array = 15", "array = 9") + "\n[ResourceZZMergedSkeleton_G1_s1]\narray = 15\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="此绘制骨骼池之外"):
        playback.export_clip(smaller_pool, None, None, extended, rig_layers=(layer,))

    with pytest.raises(ValueError, match="缺少已挂接的新增骨骼"):
        playback.export_cb1_snapshot(ini, playback.snapshot_settings("freeze", "B"))
    assert ini.read_text(encoding="utf-8") == written
    for part in ("native", "private"):
        (tmp_path / "Meshes" / f"{part}.buf").write_bytes(native_blend)
    playback.export_cb1_snapshot(ini, playback.snapshot_settings("freeze", "B"))
    snapshot = ini.read_text(encoding="utf-8")
    assert "\nfilename = Meshes/native.buf\n" in snapshot
    assert "\nfilename = ./Meshes/native.buf\n" in snapshot


def test_extension_coverage_ignores_gaps_above_original_range_and_validates_parent():
    clip = playback.BoneMatrixClip((0, 1, 4), 0, 0, 1, bytes(3 * 48), (0, 0, 1))
    layer = layered.BoneMatrixLayer(clip, {4: 1}, {}, "$active", "$frame", True, 0)
    packed = layered.pack_layers((layer,), 2, clip)
    assert packed.capacity == 5 and packed.coverage == ((0,),)
    assert packed.custom_fallback == (0xffffffff, 0xffffffff, 1)
    invalid = layered.BoneMatrixLayer(clip, {4: 3}, {}, "$active", "$frame", True, 0)
    with pytest.raises(ValueError, match="没有定义的骨骼 3"):
        layered.pack_layers((invalid,), 2, clip)
    unattached = layered.BoneMatrixLayer(clip, {}, {}, "$active", "$frame", True, 0)
    with pytest.raises(ValueError, match="新增骨骼 4 需要通过父骨链挂接"):
        layered.pack_layers((unattached,), 2, clip)
