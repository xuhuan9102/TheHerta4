import importlib
import json
import struct
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
import numpy as np

package = ModuleType("_zzmi_bone_playback_test")
package.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[package.__name__] = package
playback = importlib.import_module(package.__name__ + ".common.zzmi_bone_matrix_playback")
layered = importlib.import_module(package.__name__ + ".common.zzmi_bone_matrix_layers")
draw_calls = importlib.import_module(package.__name__ + ".common.zzmi_draw_calls")
physics = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics")

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


def test_authored_shape_and_ttl_draw_share_the_private_skinning_pass():
    draw = r"run = CommandList\TTL\Draw"
    source = INI.replace(
        "$zz_ms_occ_0 = 1", "vb0 = Resource95de0d39Position\n$zz_ms_occ_0 = 1",
    ).replace(
        "drawindexed = 3, 0, 0",
        "\n".join((r"$\TTL\_1 = 3", r"$\TTL\_2 = 0", r"$\TTL\_3 = 0", draw)),
        1,
    )
    source += "\n[CustomShader_95de0d39_Anim]\nResource95de0d39Position = ref cs-u5\n"
    result, _ = playback._configuration_text(source, "$switch", "$frame", clip())
    assert "ResourceSSMTBoneMatrixRender0MorphedPosition = copy vb0" in result
    assert "cs-t90 = ref ResourceSSMTBoneMatrixRender0MorphedPosition" in result
    assert f"; SSMT bone matrix before draw\n{draw}\n" in result
    assert result.count("\n" + draw + "\n") == 1
    assert (3, 0, 0) in draw_calls.indexed_draw_ranges(result.splitlines()).values()


def test_native_expression_capture_uses_blend_with_a_deform_guard(tmp_path):
    source = INI.replace(
        "[TextureOverride_Deform]",
        "[TextureOverride_VB_95de0d39_95de0d39_Position]\nhash = 972e4b6d\n"
        "run = CustomShaderZZMINativeExpression_95de0d39",
    ).replace(
        "[TextureOverride_Render]",
        "[TextureOverride_VB_95de0d39_95de0d39_Blend]\nhash = 4466e7ea\n\n"
        "[TextureOverride_VB_95de0d39_95de0d39_Texcoord]\nhash = 02426764\n\n"
        "[TextureOverride_Render]",
    )
    destination = tmp_path / "face.ini"
    destination.write_text(source, encoding="utf-8")
    playback.export_clip(destination, "$switch", "$frame", clip())
    result = destination.read_text(encoding="utf-8")
    position_section = result.split("[TextureOverride_VB_95de0d39_95de0d39_Position]")[1].split("[", 1)[0]
    assert "\nhash = 4466e7ea\n" in position_section
    assert "&& vb1 ==" in position_section and "filter_index = " in result
    assert position_section.index("if DRAW_TYPE == 1 && @so0 != 0") < position_section.index(
        "run = CustomShaderZZMINativeExpression"
    )
    assert position_section.index("MorphedPosition = copy vb0") < position_section.rindex("endif")
    playback.export_clip(destination, "$switch", "$frame", clip())
    assert destination.read_text(encoding="utf-8") == result
    playback.export_cb1_snapshot(destination, playback.snapshot_settings("freeze", "B"))
    restored = destination.read_text(encoding="utf-8")
    assert "[TextureOverride_VB_95de0d39_95de0d39_Position]\nhash = 972e4b6d" in restored
    assert "[TextureOverride_VB_95de0d39_95de0d39_Blend]\nhash = 4466e7ea" in restored


def test_draw_ranges_survive_material_and_tear_wrappers():
    source = r"""
[TextureOverride_Example]
run = CustomShaderTransparencyClothA
if $\TTL\enabled
    run = CommandListDragTearNativeTTL0_test
else
    drawindexed = 6, 15, 2
endif
[CustomShaderTransparencyClothA]
; --- Start of Overridden Mesh Content ---
drawindexed = 3, 12, 2
[CommandListDragTearNativeTTL0_test]
; SSMT draw range 6, 15, 2
run = CommandList\TTL\Draw
""".strip().splitlines()
    result = draw_calls.indexed_draw_ranges(source)
    assert result[1] == (3, 12, 2)
    assert result[3] == result[5] == (6, 15, 2)


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


@pytest.mark.parametrize("palette_rows", [15, 30])
def test_extended_slots_expand_only_private_palette_and_keep_native_fallback(tmp_path, palette_rows):
    asset = INI.replace("array = 15", f"array = {palette_rows}").replace(
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]",
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]\nfilename = Meshes/private.buf",
    )
    layout = {"stride": 32, "elements": [
        {"semantic": "BLENDWEIGHT", "index": 0, "format": "R32G32B32A32_FLOAT", "byte_width": 16, "offset": 0},
        {"semantic": "BLENDINDICES", "index": 0, "format": "R32G32B32A32_UINT", "byte_width": 16, "offset": 16},
    ]}
    asset = asset.replace("filename = Meshes/private.buf", "filename = Meshes/private.buf\n"
                          "; SSMT private blend layout " + json.dumps(layout))
    if palette_rows > 15:
        asset = asset.replace("; SSMT private blend layout", "; SSMT native bone slot count = 5\n; SSMT private blend layout")
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
    assert f"array = {palette_rows}" in native and "array = 21" in resolved
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
        asset.replace(f"array = {palette_rows}", "array = 9") + "\n[ResourceZZMergedSkeleton_G1_s1]\narray = 15\n",
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


def test_physics_runs_after_pose_resolution_and_before_private_skinning(tmp_path):
    def capsule(slot, parent, mode):
        return physics.Capsule(slot, parent, mode, (0, 0, 0), (0, 1, 0), (0, 0, 0),
                               .05, .1, 1., 7., 28., .35, 1.0)

    layer = layered.BoneMatrixLayer(clip(), {2: 0}, {}, "$play", "$frame", False, 5)
    bodies = capsule(0, -1, 1), capsule(2, 0, 2)
    followers = physics.build_followers(bodies, {1: 2, 2: 0}, 5)
    assert followers == (-1, 1, 1, -1, -1)
    settings = physics.BonePhysics(bodies, followers, "$physics", "$debug", "Ctrl Alt P",
                                  "Ctrl Alt O", "Ctrl Alt I", True, True, 1., 8.,
                                  step_hz=180, max_substeps=10, joint_iterations=5)
    path = tmp_path / "physics.ini"
    path.write_text(INI, encoding="utf-8")
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,), physics=settings)
    rules = path.read_text(encoding="utf-8")
    shader = rules.split("[CommandListSSMTBoneMatrixSkin0]", 1)[1].split("[", 1)[0]
    assert "$physics != 0 || $debug != 0" in shader
    assert shader.index("ResolveLayers0") < shader.index("run = CustomShaderSSMTBonePhysics0")
    assert shader.index("run = CustomShaderSSMTBonePhysics0") < shader.index("run = CustomShaderSSMTBoneMatrixSkin0")
    render = rules.split("[TextureOverride_Render]", 1)[1].split("[", 1)[0]
    assert render.index("\ndrawindexed =") < render.index("run = CommandListSSMTBonePhysicsPreview0")
    present = rules.split("[Present]", 1)[1].split("[", 1)[0]
    assert "BonePhysicsPreview" not in present
    overlay = rules.split("[CustomShaderSSMTBonePhysicsPreview0]", 1)[1].split("\n[", 1)[0]
    assert "o0 = " not in overlay
    assert "mask[1] = 0" in overlay
    assert "y47 = ($physics != 0)" in rules
    assert "z47 = 0" in rules
    assert "x48 = 180" in rules and "y48 = 10" in rules and "z48 = 5" in rules
    assert "w48 = 27148" in rules and "w48 = $ssmt_bm_physics_w48" in rules
    assert (tmp_path / "res/SSMTBonePhysicsStateInit.buf").stat().st_size == settings.state_rows * 16
    assert "cs-t99 = ref ResourceSSMTBonePhysicsAirDrag" in rules
    assert "cs-t99 = ref ResourceSSMTBoneMatrixRenderSavedt99" in rules
    assert "w47 = 27149" in rules
    air_drag = np.fromfile(tmp_path / "res/SSMTBonePhysicsAirDrag.buf", "<f4")
    np.testing.assert_allclose(air_drag, [body.air_drag for body in bodies])
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,), physics=settings)
    assert path.read_text(encoding="utf-8") == rules
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,),
                         physics=replace(settings, use_substeps=False, frame_steps=2))
    fixed_frame = path.read_text(encoding="utf-8")
    assert "x48 = 0" in fixed_frame and "y48 = -2" in fixed_frame
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,), physics=replace(
        settings, use_substeps=False, frame_steps=5, joint_iterations=1, joints_per_frame=True,
    ))
    frame_joints = path.read_text(encoding="utf-8")
    assert "y48 = -5" in frame_joints and "z48 = -1" in frame_joints
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,), physics=replace(settings, adaptive_substeps=True))
    assert "x48 = -180" in path.read_text(encoding="utf-8")
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,))
    assert "CommandListSSMTBonePhysics" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("weight, expect_physics", [(0., False), (.5, True), (None, True)])
def test_physics_skips_draws_with_verified_unaffected_weights(tmp_path, weight, expect_physics):
    source = INI.replace(
        "[ResourceSSMTBoneMatrixBindPosition_95de0d39]\ntype = StructuredBuffer\narray = 3",
        "[ResourceSSMTBoneMatrixBindPosition_95de0d39]\ntype = StructuredBuffer\narray = 4",
    ).replace(
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]\ntype = StructuredBuffer\narray = 3",
        "[ResourceSSMTBoneMatrixBindBlend_95de0d39]\ntype = StructuredBuffer\narray = 4\nfilename = reference-blend.buf",
    ) + f"""
[TextureOverride_DeformOther]
$zz_ms_occ_1 = 1
if $zz_ms_occ_1 == 1
    ResourceZZPalette_e35322ac_s1 = copy vs-t0 unless_null
endif
vs-t0 = {PALETTE}

[ResourceSSMTBoneMatrixBindPosition_e35322ac]
type = StructuredBuffer
array = 3
[ResourceSSMTBoneMatrixBindBlend_e35322ac]
type = StructuredBuffer
array = 3
"""
    (tmp_path / "reference-blend.buf").write_bytes(struct.pack("<4f4I", 1., 0., 0., 0., 0, 2, 0, 0) * 4)
    if weight is not None:
        source += "filename = other-blend.buf\n"
        (tmp_path / "other-blend.buf").write_bytes(
            struct.pack("<4f4I", 1. - weight, weight, 0., 0., 0, 1, 2, 0) * 3,
        )
    settings = physics.BonePhysics(
        (physics.Capsule(2, -1, 2, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                         .05, .1, 1., 7., 28., .35, 1.),),
        (-1, 0, 0, -1, -1), "$physics", "$debug", "", "", "", True, True, 1., 8.,
    )
    layer = layered.BoneMatrixLayer(clip(), {2: 0}, {}, "$play", "$frame", True, 5)
    path = tmp_path / "model.ini"
    path.write_text(source, encoding="utf-8")
    playback.export_clip(path, None, None, clip(), rig_layers=(layer,), physics=settings)
    rules = path.read_text(encoding="utf-8")
    assert "[CustomShaderSSMTBonePhysics0]" in rules
    assert "[CommandListSSMTBonePhysicsPreview0]" in rules
    assert ("[CustomShaderSSMTBonePhysics1]" in rules) == expect_physics
    assert "[CustomShaderSSMTBoneMatrixSkin1]" in rules
    other = rules.split("[TextureOverride_OtherCostume]", 1)[1].split(f"[{PALETTE}]", 1)[0]
    assert "$physics != 0" in other
    assert ("ResourceSSMTBoneMatrixRender1ObjectCB = copy vs-cb1" in other) == expect_physics


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_substep_budget_can_cover_slow_render_frames():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    body = physics.Capsule(0, -1, 2, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                           .04, .1, .4, 3., 0., .3, 1.)

    def fall(render_hz, max_substeps):
        settings = physics.BonePhysics((body,), (0,), "$physics", "$debug", "", "", "",
                                       True, False, 1., 0., max_substeps=max_substeps)
        solver = renderer.PreviewSolver(settings)
        source = np.eye(4, dtype="<f4")[None, :3].copy()
        try:
            for frame in range(render_hz // 5 + 1):
                result = solver.step(source, frame / render_hz, frame, 0, 1., 0.)
            return float(result[0, 2, 3])
        finally:
            solver.close()

    regular = fall(60, 8)
    slow_with_budget = fall(20, 8)
    slow_with_legacy_limit = fall(20, 4)
    assert regular < -.1
    assert abs(regular - slow_with_budget) < 2e-5
    assert abs(regular - slow_with_legacy_limit) > .04


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("frame_steps", (1, 2))
def test_fixed_frame_steps_integrate_the_actual_frame_duration(frame_steps):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    body = physics.Capsule(0, -1, 2, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                           .04, .1, .4, 0., 0., .3, 1.)
    settings = physics.BonePhysics((body,), (0,), "$physics", "$debug", "", "", "",
                                    True, False, 1., 0., use_substeps=False, frame_steps=frame_steps)
    source = np.eye(4, dtype="<f4")[None, :3].copy()
    solver = renderer.PreviewSolver(settings)
    try:
        solver.step(source, 0., 0, 0, 1., 0.)
        elapsed = .035
        result = solver.step(source, elapsed, 1, 0, 1., 0.)
        expected = -9.81 * elapsed ** 2 * (frame_steps + 1) / (2 * frame_steps)
        np.testing.assert_allclose(result[0, 2, 3], expected, atol=2e-6)
        repeated = solver.step(source, elapsed, 1, 0, 1., 0.)
        np.testing.assert_array_equal(repeated, result)
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_adaptive_substeps_keep_one_step_until_the_attachment_settles():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    arm = physics.Capsule(1, 0, 2, (.2, 0., 0.), (1., 0., 0.), (0., 0., 0.),
                          .04, .14, .3, 3., 8., .3, 1.2, pin_translation=True)
    settings = physics.BonePhysics((anchor, arm), (-1, 1), "$physics", "$debug", "", "", "",
                                    True, False, .8, 0., adaptive_substeps=True)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    size = settings.state_rows * 16
    staging = solver._buffer(None, size=size, usage=3, access=0x20000)

    def sample(frame):
        solver.step(source, frame / 60, frame, 0, .8, 0.)
        renderer._call(solver.context, 47, None, (renderer.P, renderer.P), staging, solver.state)
        mapped = renderer.Mapping()
        renderer._check(renderer._call(
            solver.context, 14, renderer.HR,
            (renderer.P, renderer.U, renderer.U, renderer.U, renderer.P),
            staging, 0, 1, 0, renderer.ct.byref(mapped),
        ))
        try:
            state = np.frombuffer(renderer.ct.string_at(mapped.pointer, size), "<f4").reshape(-1, 4)
        finally:
            renderer._call(solver.context, 15, None, (renderer.P, renderer.U), staging, 0)
        reset, _, _, steps = state[2 + 6 * len(settings.capsules)]
        assert reset == float(frame == 0)
        return int(steps), int(state[-2, 0])

    try:
        sample(0)
        assert sample(1) == (2, 0)
        for frame in range(2, 8):
            source[:, 0, 3] += .025
            assert sample(frame) == (1, 1)
        for frame in range(8, 16):
            assert sample(frame) == (1, 1)
        for frame in range(16, 25):
            result = sample(frame)
        assert result == (2, 0)
        angle = .12
        source[:, :, :3] = ((np.cos(angle), -np.sin(angle), 0.),
                           (np.sin(angle), np.cos(angle), 0.), (0., 0., 1.))
        assert sample(25) == (1, 1)
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("joints_per_frame, expected_passes", ((True, 1), (False, 5)))
def test_joint_schedule_counts_passes_across_five_collision_steps(joints_per_frame, expected_passes):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    arm = physics.Capsule(1, 0, 2, (.2, 0., 0.), (1., 0., 0.), (0., 0., 0.),
                          .04, .14, .3, 3., 8., .3, 1.2, pin_translation=True)
    settings = physics.BonePhysics(
        (anchor, arm), (-1, 1), "$physics", "$debug", "", "", "", True, False, .8, 0.,
        joint_iterations=1, use_substeps=False, frame_steps=5, joints_per_frame=joints_per_frame,
    )
    solver = renderer.PreviewSolver(settings)
    try:
        source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
        solver.step(source, 0., 0, 0, .8, 0.)
        source[:, 0, 3] = .12
        result = solver.step(source, 1. / 30, 1, 0, .8, 0.)
        np.testing.assert_allclose(result[1, :, 3], source[0, :, 3], atol=2e-6)
        size = settings.state_rows * 16
        staging = solver._buffer(None, size=size, usage=3, access=0x20000)
        renderer._call(solver.context, 47, None, (renderer.P, renderer.P), staging, solver.state)
        mapped = renderer.Mapping()
        renderer._check(renderer._call(
            solver.context, 14, renderer.HR,
            (renderer.P, renderer.U, renderer.U, renderer.U, renderer.P),
            staging, 0, 1, 0, renderer.ct.byref(mapped),
        ))
        try:
            state = np.frombuffer(renderer.ct.string_at(mapped.pointer, size), "<f4").reshape(-1, 4)
        finally:
            renderer._call(solver.context, 15, None, (renderer.P, renderer.U), staging, 0)
        assert state[2 + 6 * len(settings.capsules), 3] == 5
        assert state[-1, 0] == expected_passes
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_three_layer_field_fades_between_rigid_and_collision_capsules():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")

    def capsule(slot, mode):
        return physics.Capsule(slot, -1, mode, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                               .1, .08, .4, 4., 0., .3, 1.2)

    capsules = capsule(0, 1), capsule(1, 2)
    settings = physics.BonePhysics(capsules, physics.build_followers(capsules, {}, 2),
                                   "$physics", "$debug", "", "", "", True, False, 0., 8.)

    def separation_change(distance):
        source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
        source[0, 0, 3] = -distance
        solver = renderer.PreviewSolver(settings)
        try:
            solver.step(source, 0., 0, 0, 0., 8.)
            posed = solver.step(source, 1. / 60, 1, 0, 0., 8.)
            return float(posed[1, 0, 3] - posed[0, 0, 3] - distance)
        finally:
            solver.close()

    assert abs(separation_change(.205)) < 2e-6
    edge = separation_change(.197)
    middle = separation_change(.175)
    assert middle > edge * 3 > 0.0
    assert abs(separation_change(.14)) < 2e-6


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_overlapping_rigid_capsules_do_not_repel_each_other():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    bodies = tuple(physics.Capsule(slot, -1, 2, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                                   .1, .08, .4, 4., 0., .3, 1.2) for slot in range(2))
    settings = physics.BonePhysics(bodies, (0, 1), "$physics", "$debug", "", "", "", True, False, 0., 8.)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    source[0, 0, 3] = -.175
    solver = renderer.PreviewSolver(settings)
    try:
        solver.step(source, 0., 0, 0, 0., 8.)
        np.testing.assert_allclose(solver.step(source, 1 / 60, 1, 0, 0., 8.), source, atol=2e-6)
    finally:
        solver.close()


def test_soft_link_buffer_uses_bind_length_and_rejects_duplicate_endpoints():
    first = physics.Capsule(3, -1, 2, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                            .04, .08, .1, 5., 0., .3, 1., twist_limit=.2)
    second = replace(first, slot=8, center=(.25, 0., 0.), twist_limit=None)
    link = physics.SoftLink(8, 3, 200., 8.)
    settings = physics.BonePhysics((first, second), (), "$physics", "$debug", "", "", "",
                                   True, False, 1., 0., soft_links=(link,))
    packed = dict(settings.assets())["SSMTBonePhysicsJointOptions.buf"]
    np.testing.assert_allclose(np.frombuffer(packed, "<f4").reshape(-1, 4), [
        (2, 1, 0, 0), (.2, 0, 0, 0), (-1, 0, 0, 0), (0, 1, .25, 200), (8, 0, 0, 0),
    ])
    with pytest.raises(ValueError, match="重复"):
        replace(settings, soft_links=(link, replace(link, first=3, second=8))).assets()


@pytest.mark.parametrize(("bodies", "links", "prefix"), [
    (30, 24, b""),
    (102, 96, b"#define SSMT_PHYSICS_BODIES 128\n#define SSMT_PHYSICS_LINKS 128\n"),
])
def test_bone_physics_shader_capacity_matches_packed_rig(bodies, links, prefix):
    assets = (
        ("Bodies", bytes(80 * bodies)), ("Followers", b""), ("State", b""), ("AirDrag", b""),
        ("JointOptions", struct.pack("<4f", bodies, links, 3, 0)),
    )
    assert physics.solver_shader_source(b"source", assets) == prefix + b"source"


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_soft_link_transfers_motion_between_separate_bone_chains():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    rigid = physics.Capsule(1, -1, 2, (.2, 0., 0.), (0., 0., 1.), (.2, 0., 0.),
                            .04, .08, .1, 5., 0., .3, 1.)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    settings = physics.BonePhysics((anchor, rigid), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 0., use_substeps=False, frame_steps=2,
                                   joint_iterations=3, joints_per_frame=True)
    for links in ((), (physics.SoftLink(0, 1, 160., 8.),)):
        solver = renderer.PreviewSolver(replace(settings, soft_links=links))
        try:
            source[0, 0, 3] = 0.
            solver.step(source, 0., 0, 0, 0., 0.)
            source[0, 0, 3] = -.12
            result = solver.step(source, 1 / 30, 1, 0, 0., 0.)
            if links:
                assert result[1, 0, 3] < -.01
            else:
                np.testing.assert_allclose(result[1, 0, 3], 0., atol=1e-6)
        finally:
            solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_swept_collider_uses_the_soft_field_and_yields_to_deep_overlap():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    collider = physics.Capsule(0, -1, 1, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                               .1, .08, 1., 0., 0., 0., 1., sweep=True)
    rigid = replace(collider, slot=1, mode=2, sweep=False)
    settings = physics.BonePhysics((collider, rigid), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 8., use_substeps=False)
    for depth, expect_contact in ((.175, True), (.13, False)):
        solver = renderer.PreviewSolver(settings)
        source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
        source[0, :2, 3] = (-.25, -depth)
        try:
            solver.step(source, 0., 0, 0, 0., 8.)
            source[0, 0, 3] = .25
            posed = solver.step(source, 1 / 60, 1, 0, 0., 8.)
            assert (posed[1, 1, 3] > 1e-5) == expect_contact
        finally:
            solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("initial_x, repels", [(-.25, True), (-.12, False)])
def test_sweep_catches_entry_to_deep_overlap_without_pushing_an_existing_overlap(initial_x, repels):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    collider = physics.Capsule(0, -1, 1, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                               .1, .08, 1., 0., 0., 0., 1., sweep=True)
    box = physics.RoundedBox((.08, .08, .04), (1., 0., 0.), .014)
    skirt = replace(collider, slot=1, mode=2, radius=.04, half_segment=0., mass=.08,
                    sweep=False, box=box, air_drag=0.)
    settings = physics.BonePhysics((collider, skirt), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 8., use_substeps=False)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    source[0, 0, 3] = initial_x
    solver = renderer.PreviewSolver(settings)
    try:
        solver.step(source, 0., 0, 0, 0., 8.)
        source[0, 0, 3] = -.075
        posed = solver.step(source, 1 / 60, 1, 0, 0., 8.)
        assert (posed[1, 0, 3] > 1e-5) == repels
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("allow_deep_overlap", [True, False])
def test_skirt_can_keep_contact_active_in_deep_overlap(allow_deep_overlap):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    thigh = physics.Capsule(0, -1, 1, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                            .1, .08, 1., 0., 0., 0., 1., sweep=True)
    panel = physics.Capsule(1, -1, 2, (.08, 0., 0.), (0., 0., 1.), (.08, 0., 0.),
                            .025, 0., .08, 0., 0., 0., 1., air_drag=0.,
                            box=physics.RoundedBox((.05, .08, .025), (1., 0., 0.), .01),
                            allow_deep_overlap=allow_deep_overlap)
    settings = physics.BonePhysics((thigh, panel), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 6., use_substeps=False)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    solver = renderer.PreviewSolver(settings)
    try:
        solver.step(source, 0., 0, 0, 0., 6.)
        posed = solver.step(source, 1 / 60, 1, 0, 0., 6.)
        shift = ((posed[1] - source[1]) @ np.r_[panel.center, 1.])[0]
        if allow_deep_overlap:
            assert abs(shift) < 1e-6
        else:
            assert .005 < shift < .08
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("offset_x, angle, repel", [(.172, 0, True), (.172, 90, False), (.035, 0, False)])
def test_rounded_box_has_oriented_coverage_and_yields_to_deep_overlap(offset_x, angle, repel):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    box = physics.RoundedBox((.16, .18, .14), (1., 0., 0.), .02)
    collider = physics.Capsule(0, -1, 1, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                               .14, 0., 1., 0., 0., 0., 1., box=box)
    skirt = physics.Capsule(1, -1, 2, (offset_x, .09, 0.), (0., 0., 1.), (offset_x, .09, 0.),
                            .026, .044, .1, 0., 0., 0., 1., air_drag=0.)
    settings = physics.BonePhysics((collider, skirt), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 8., use_substeps=False)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    source[0] = _rotation(angle)
    try:
        solver.step(source, 0., 0, 0, 0., 8.)
        posed = solver.step(source, 1 / 60, 1, 0, 0., 8.)
        center = np.r_[skirt.center, 1.]
        separation = ((posed[1] - source[1]) @ center)[0]
        assert (separation > 1e-6) == repel
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("collider_box", [None, physics.RoundedBox((.086, .15, .085), (1., 0., 0.), .014)])
@pytest.mark.parametrize("offset, repels", [(.128, True), (.02, False)])
def test_dynamic_rounded_box_contacts_capsules_and_boxes(collider_box, offset, repels):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    collider = physics.Capsule(0, -1, 1, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                               .086, .06, 1., 0., 0., 0., 1., box=collider_box)
    panel = physics.RoundedBox((.05, .08, .024), (1., 0., 0.), .009)
    dynamic = physics.Capsule(1, -1, 2, (offset, .013, 0.), (0., 0., 1.), (offset, .013, 0.),
                              .024, 0., .1, 0., 0., 0., 1., air_drag=0., box=panel)
    settings = physics.BonePhysics((collider, dynamic), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 8., use_substeps=False)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    try:
        solver.step(source, 0., 0, 0, 0., 8.)
        posed = solver.step(source, 1 / 60, 1, 0, 0., 8.)
        shift = ((posed[1] - source[1]) @ np.r_[dynamic.center, 1.])[0]
        assert np.isfinite(posed).all()
        assert (shift > 1e-6) == repels
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_axial_twist_is_limited_relative_to_the_animated_parent():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    rigid = physics.Capsule(1, 0, 2, (0., 0., -.12), (0., 0., 1.), (0., 0., 0.),
                            .04, .08, .1, 0., 0., .3, 1.5, twist_limit=.18)
    settings = physics.BonePhysics((anchor, rigid), (-1, 1), "$physics", "$debug", "", "", "",
                                   True, False, 0., 0., use_substeps=False)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    try:
        solver.step(source, 0., 0, 0, 0., 0.)
        source[:] = _rotation(60)
        result = solver.step(source, 1 / 60, 1, 0, 0., 0.)
        relative = result[0, :, :3].T @ result[1, :, :3]
        assert abs(np.arctan2(relative[1, 0], relative[0, 0])) <= .18001
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_three_layer_field_resumes_when_a_deep_contact_backs_out():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    bodies = tuple(physics.Capsule(slot, -1, mode, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                                   .1, .08, .4, 4., 0., .3, 1.2) for slot, mode in ((0, 1), (1, 2)))
    settings = physics.BonePhysics(bodies, (-1, 1), "$physics", "$debug", "", "", "", True, False, 0., 8.)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    try:
        solver.step(source, 0., 0, 0, 0., 8.)
        deep = solver.step(source, 1 / 60, 1, 0, 0., 8.)
        np.testing.assert_allclose(deep, source, atol=2e-6)
        source[0, 0, 3] = -.175
        shallow = solver.step(source, 2 / 60, 2, 0, 0., 8.)
        assert shallow[1, 0, 3] > 0.
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_rigid_joint_swings_on_acceleration_and_releases_at_running_speed():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    arm = physics.Capsule(1, 0, 2, (0., 0., -.22), (0., 0., 1.), (0., 0., 0.),
                          .035, .18, .25, 20., 80., .3, 1., air_drag=0.)
    settings = physics.BonePhysics(
        (anchor, arm), (-1, 1), "$physics", "$debug", "", "", "", True, False, 0., 0.,
        joint_iterations=3, use_substeps=False, frame_steps=2, joints_per_frame=True,
    )
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    solver = renderer.PreviewSolver(settings)
    swing = []
    try:
        speed = 6.
        for frame in range(180):
            elapsed = frame / 60.
            source[:, 0, 3] = speed * (elapsed ** 2 if elapsed <= .5 else elapsed - .25)
            posed = solver.step(source, elapsed, frame, 0, 0., 0.)
            swing.append(abs(posed[1, 0, 2]))
        assert max(swing[:45]) > .4
        assert max(swing[-30:]) < .2
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_air_drag_lifts_a_hanging_capsule_at_higher_speed():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    ribbon = physics.Capsule(1, 0, 2, (0., 0., -.22), (0., 0., 1.), (0., 0., 0.),
                             .035, .18, .04, 10., 20., .3, 1.5, air_drag=.6)
    settings = physics.BonePhysics(
        (anchor, ribbon), (-1, 1), "$physics", "$debug", "", "", "", True, False, .45, 0.,
        joint_iterations=3, use_substeps=False, frame_steps=2, joints_per_frame=True,
    )

    def settled_center(speed):
        source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
        solver = renderer.PreviewSolver(settings)
        try:
            for frame in range(180):
                elapsed = frame / 60.
                source[:, 0, 3] = speed * (elapsed ** 2 if elapsed <= .5 else elapsed - .25)
                posed = solver.step(source, elapsed, frame, 0, .45, 0.)
            return (posed[1] - source[1]) @ (*ribbon.center, 1.)
        finally:
            solver.close()

    still, slow, fast = (settled_center(speed) for speed in (0., 1.8, 5.5))
    assert fast[0] < slow[0] < still[0]
    assert fast[2] > slow[2] > still[2]


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_one_joint_pass_corrects_an_off_axis_attachment_error():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    anchor = physics.Capsule(0, -1, 0, (0., 0., 0.), (1., 0., 0.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    arm = physics.Capsule(1, 0, 2, (.12, .04, .03), (1., 0., 0.), (0., 0., 0.),
                          .035, .12, .3, 10., 0., .3, 1.2)
    settings = physics.BonePhysics(
        (anchor, arm), (-1, 1), "$physics", "$debug", "", "", "", True, False, 0., 0.,
        joint_iterations=1, use_substeps=False, frame_steps=2, joints_per_frame=True,
    )
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    solver = renderer.PreviewSolver(settings)
    try:
        solver.step(source, 0., 0, 0, 0., 0.)
        source[:, :, 3] = (.008, .007, .006)
        posed = solver.step(source, 1 / 30, 1, 0, 0., 0.)
        assert np.linalg.norm(posed[1, :, 3] - posed[0, :, 3]) < 1e-4
    finally:
        solver.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
def test_pinned_shoulder_preserves_original_rigid_rotations():
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    parent = physics.Capsule(2, -1, 0, (0., 0., 0.), (0., 0., 1.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    shoulder = physics.Capsule(1, 2, 2, (.29, 0., 0.), (1., 0., 0.), (.16, 0., 0.),
                                .035, .12, .5, 3., 120., .3, 1.)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 3, axis=0)
    results = []
    for pin in (False, True):
        bodies = replace(shoulder, pin_translation=pin), parent
        settings = physics.BonePhysics(bodies, (-1, 0, -1), "$physics", "$debug", "", "", "",
                                        True, False, .45, 0., joint_iterations=1)
        solver = renderer.PreviewSolver(settings)
        try:
            sequence = []
            for frame in range(40):
                source[:, 1, 3] = .060 * min(frame, 30)
                sequence.append(solver.step(source, frame / 60, frame, 0, .45, 0.))
            results.append(np.stack(sequence))
        finally:
            solver.close()

    unpinned, pinned = results
    joint = (.16, 0., 0., 1.)
    assert np.max(np.linalg.norm((unpinned[:, 1] - unpinned[:, 2]) @ joint, axis=-1)) > 1e-4
    np.testing.assert_allclose(pinned[:, 1] @ joint, pinned[:, 2] @ joint, atol=2e-6)
    np.testing.assert_allclose(pinned[..., :3], unpinned[..., :3], atol=2e-6)


@pytest.mark.skipif(sys.platform != "win32", reason="Uses the DX11 WARP preview solver")
@pytest.mark.parametrize("delay", [1 / 60, .22])
def test_physics_keeps_the_pose_across_a_sudden_attachment_move(delay):
    renderer = importlib.import_module(package.__name__ + ".common.zzmi_bone_physics_preview_dx11")
    parent = physics.Capsule(0, -1, 0, (0., 0., 0.), (0., 1., 0.), (0., 0., 0.),
                             0., 0., 0., 0., 0., 0., 0.)
    arm = physics.Capsule(1, 0, 2, (.2, 0., 0.), (1., 0., 0.), (0., 0., 0.),
                          .04, .14, .3, 3., 8., .3, 1.2, pin_translation=True)
    settings = physics.BonePhysics((parent, arm), (-1, 1), "$physics", "$debug", "", "", "",
                                    True, False, .8, 0.)
    solver = renderer.PreviewSolver(settings)
    source = np.repeat(np.eye(4, dtype="<f4")[None, :3], 2, axis=0)
    try:
        for frame in range(40):
            posed = solver.step(source, frame / 60, frame, 0, .8, 0.)
        assert abs(posed[1, 2, 0]) > .1
        source[:, 1, 3] = .96
        moved = solver.step(source, frame / 60 + delay, frame + 1, 0, .8, 0.)
        assert abs(moved[1, 2, 0]) > .08
        np.testing.assert_allclose(moved[1, :, 3], source[0, :, 3], atol=3e-6)
        if delay > .15:
            np.testing.assert_allclose(moved[1, :, :3], posed[1, :, :3], atol=3e-6)
    finally:
        solver.close()
