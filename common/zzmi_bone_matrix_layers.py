"""Build independently controlled animation tracks in the global bone space."""

from dataclasses import dataclass

import numpy as np


LAYER_CONTROL = "ResourceSSMTBoneMatrixLayerControl"
LAYER_CONTROL_WRITE = "ResourceSSMTBoneMatrixLayerControlWrite"
LAYER_UPDATE = "CommandListSSMTBoneMatrixUpdateLayers"
LAYER_FULL = "$ssmt_bm_layers_full"
LAYER_EPOCH = "$ssmt_bone_matrix_calibration_epoch"
LAYER_FALLBACK = "ResourceSSMTBoneMatrixLayerFallback"
_TAG = "SSMT bone matrix"
_INVALID = 0xFFFFFFFF


@dataclass(frozen=True)
class BoneMatrixLayer:
    clip: object
    hierarchy: dict
    source_hierarchy: dict
    play: str
    frame: str
    initial_play: bool
    initial_frame: int
    key: str = ""


@dataclass(frozen=True)
class LayeredPose:
    layers: tuple
    capacity: int
    coverage: tuple
    assets: tuple
    tracks: int
    native_capacity: int
    custom_fallback: tuple

    @property
    def enabled(self):
        return "(" + " || ".join(f"{layer.play} != 0" for layer in self.layers) + ")"

    def definitions(self, shader):
        lines = ["", f"; {_TAG} begin layers"]
        for index, layer in enumerate(self.layers):
            if layer.key:
                lines.extend((
                    f"[KeySSMTBoneMatrixLayer{index}]", f"key = {layer.key}", "type = cycle",
                    f"{layer.play} = 0,1", "",
                ))
        for name, fmt, filename in (
            ("LayerRanges", "R32G32_UINT", "SSMTBoneMatrixLayerRanges.buf"),
            ("LayerCandidates", "R32_UINT", "SSMTBoneMatrixLayerCandidates.buf"),
            ("LayerTracks", "R32G32_UINT", "SSMTBoneMatrixLayerTracks.buf"),
        ):
            lines.extend((
                f"[ResourceSSMTBoneMatrix{name}]", "type = Buffer", f"format = {fmt}",
                f"filename = res/{filename}", "",
            ))
        if self.custom_fallback:
            lines.extend((
                f"[{LAYER_FALLBACK}]", "type = Buffer", "format = R32_UINT",
                "filename = res/SSMTBoneMatrixLayerFallback.buf", "",
            ))
        lines.extend((
            f"[{LAYER_CONTROL}]", "type = RWBuffer", "format = R32G32B32A32_FLOAT",
            "bind_flags = shader_resource unordered_access", "filename = res/SSMTBoneMatrixLayerControl.buf", "",
            f"[{LAYER_CONTROL_WRITE}]", "type = Buffer", "format = R32_FLOAT",
            "stride = 4", "",
            "[ResourceSSMTBoneMatrixLayerSavedU7]", "",
            "[CustomShaderSSMTBoneMatrixWriteLayerControl]", f"cs = res/{shader}",
            f"{LAYER_CONTROL_WRITE} = ref {LAYER_CONTROL}", "ResourceSSMTBoneMatrixLayerSavedU7 = ref cs-u7",
            f"cs-u7 = ref {LAYER_CONTROL_WRITE}", "Dispatch = 1, 1, 1",
            "cs-u7 = ref ResourceSSMTBoneMatrixLayerSavedU7", "ResourceSSMTBoneMatrixLayerSavedU7 = null", "",
            f"[{LAYER_UPDATE}]", "if $ssmt_bm_layers_seen_frame != frame_number",
            "    $ssmt_bm_layers_seen_frame = frame_number",
            "    local $ssmt_bm_layers_x43 = x43", "    local $ssmt_bm_layers_y43 = y43",
            "    local $ssmt_bm_layers_z43 = z43", "    local $ssmt_bm_layers_w43 = w43",
        ))
        for index, layer in enumerate(self.layers):
            enabled = f"$ssmt_bm_layer_seen_play_{index}"
            frame = f"$ssmt_bm_layer_seen_frame_{index}"
            lines.extend((
                f"    if {enabled} != ({layer.play} != 0) || {frame} != {layer.frame}",
                f"        x43 = {index}", f"        y43 = ({layer.play} != 0)",
                f"        z43 = {layer.frame} - ({layer.clip.first_frame})", "        w43 = 35983",
                "        run = CustomShaderSSMTBoneMatrixWriteLayerControl",
                f"        {enabled} = ({layer.play} != 0)", f"        {frame} = {layer.frame}",
                "    endif",
            ))
        lines.extend((
            "    x43 = $ssmt_bm_layers_x43", "    y43 = $ssmt_bm_layers_y43",
            "    z43 = $ssmt_bm_layers_z43", "    w43 = $ssmt_bm_layers_w43",
        ))
        if self.coverage:
            lines.extend((f"    local $ssmt_bm_previous_full = {LAYER_FULL}", f"    {LAYER_FULL} = 1"))
            for group in self.coverage:
                off = " && ".join(f"{self.layers[index].play} == 0" for index in group)
                lines.extend((f"    if {off}", f"        {LAYER_FULL} = 0", "    endif"))
            lines.append(f"    {LAYER_EPOCH} = {LAYER_EPOCH} + ($ssmt_bm_previous_full == 0 && {LAYER_FULL} != 0)")
        lines.extend(("endif", f"; {_TAG} end"))
        return lines

    def constants(self, default_marker):
        lines = ["global $ssmt_bm_layers_seen_frame = -1"]
        if self.coverage:
            lines.append(f"global {LAYER_FULL} = 0")
        for index, layer in enumerate(self.layers):
            lines.extend((
                default_marker, f"global {layer.play} = {int(layer.initial_play)}",
                default_marker, f"global {layer.frame} = {layer.initial_frame}",
                f"global $ssmt_bm_layer_seen_play_{index} = -1",
                f"global $ssmt_bm_layer_seen_frame_{index} = 0",
            ))
        return lines


def _custom_fallback(candidates, tracks, native_capacity):
    """Resolve each extra slot to a native ancestor for ordinary deform draws."""
    result = {}
    for slot in range(native_capacity, len(candidates)):
        if not candidates[slot]:
            continue
        chain = set()
        parent = slot
        while parent >= native_capacity:
            if parent in result:
                parent = result[parent]
                break
            if parent in chain or not candidates[parent]:
                raise ValueError(f"新增骨骼 {slot} 的父骨链存在循环或引用了没有定义的骨骼 {parent}")
            chain.add(parent)
            parent = tracks[candidates[parent][-1]][1]
        result.update((child, parent) for child in chain)
    return tuple(result.get(slot, _INVALID) for slot in range(native_capacity, len(candidates)))


def pack_layers(layers, native_capacity, reference_clip):
    tracks = []
    frames = []
    capacity = max(native_capacity, max(slot for layer in layers for slot in layer.clip.slots) + 1)
    candidates = [[] for _ in range(capacity)]
    for layer_index, layer in enumerate(layers):
        clip = layer.clip
        if (clip.first_frame, clip.frame_count) != (reference_clip.first_frame, reference_clip.frame_count):
            raise ValueError("多个骨架需要使用相同的采样帧范围")
        poses = np.frombuffer(clip.matrices, "<f4").reshape(clip.frame_count, len(clip.slots), 3, 4)
        pose_indices = {slot: track for track, slot in enumerate(clip.slots)}
        for track, slot in enumerate(clip.slots):
            if not 0 <= slot < capacity:
                raise ValueError(f"骨架替换编号 {slot} 超出导出的全局顶点组范围")
            parent = layer.hierarchy.get(slot, -1)
            if parent >= capacity:
                raise ValueError(f"父骨编号 {parent} 超出导出的全局顶点组范围")
            pose = poses[:, track]
            source = pose_indices.get(layer.source_hierarchy.get(slot))
            if source is not None:
                source_pose = poses[:, source]
                rhs = pose.copy()
                rhs[:, :, 3] -= source_pose[:, :, 3]
                try:
                    pose = np.linalg.solve(source_pose[:, :, :3], rhs)
                except np.linalg.LinAlgError as error:
                    raise ValueError(f"编号 {slot} 的原父骨矩阵不可逆，无法导出局部动画") from error
            candidates[slot].append(len(tracks))
            tracks.append((layer_index, parent if parent >= 0 else _INVALID))
            frames.append(pose)

    spans = []
    ordered = []
    for options in candidates:
        spans.append((len(ordered), len(options)))
        ordered.extend(reversed(options))
    for _, parent in tracks:
        if native_capacity <= parent < capacity and not candidates[parent]:
            raise ValueError(f"父骨链引用了没有定义的骨骼 {parent}")
    for slot in range(native_capacity, capacity):
        if any(tracks[track][1] == _INVALID for track in candidates[slot]):
            raise ValueError(f"新增骨骼 {slot} 需要通过父骨链挂接到原生骨骼")
    coverage = ()
    native_candidates = candidates[:native_capacity]
    if reference_clip.pivot is not None and 0 <= reference_clip.anchor < native_capacity and all(native_candidates):
        possible = {tuple(sorted({tracks[track][0] for track in choices})) for choices in native_candidates}
        coverage = tuple(sorted(group for group in possible if not any(
            set(other) < set(group) for other in possible
        )))

    def packed(name, values, dtype):
        return name, np.asarray(values, dtype=dtype).tobytes()

    fallback = _custom_fallback(candidates, tracks, native_capacity)
    assets = (
        packed("SSMTBoneMatrixLayerRanges.buf", spans, "<u4"),
        packed("SSMTBoneMatrixLayerCandidates.buf", ordered, "<u4"),
        packed("SSMTBoneMatrixLayerTracks.buf", tracks, "<u4"),
        packed("SSMTBoneMatrixLayerControl.buf", np.zeros((len(layers), 4)), "<f4"),
        packed("SSMTBoneMatrixFrames.buf", frames, "<f4"),
    )
    if fallback:
        assets += (packed("SSMTBoneMatrixLayerFallback.buf", fallback, "<u4"),)
    return LayeredPose(tuple(layers), capacity, coverage, assets, len(tracks), native_capacity, fallback)
