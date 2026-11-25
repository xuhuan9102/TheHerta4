"""Emit an IB-scoped ZZMI pose override and its baked frame data."""

import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .safe_write import write_bytes_if_changed_atomic
from .zzmi_bone_matrix_layers import pack_layers
from .zzmi_bone_matrix_snapshot import CB1Snapshot, hotkey_value
from .zzmi_bone_matrix_render_calibration import (
    RENDER_SHADER,
    SKIN_SHADER,
    SUPPORT_FILE,
    add_scoped_playback,
    bind_native_blends,
    prepare_native_blends,
    read_ground_support,
)
from .zzmi_bone_matrix_render_inputs import NATIVE_SLOT_COUNT_MARKER
from .zzmi_bone_physics import PREVIEW_PIXEL_SHADER, PREVIEW_SHADER, SHADER as PHYSICS_SHADER, solver_shader_source

_TAG = "SSMT bone matrix"
_BEGIN = rf"; {_TAG} begin"
_END = rf"; {_TAG} end"
_BLOCK = re.compile(rf"(?ms)^[ \t]*{_BEGIN}[^\n]*\n.*?^[ \t]*{_END}[^\n]*\n?")
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$", re.IGNORECASE)
_GLOBAL = re.compile(r"^\s*global(?:\s+persist)?\s+(\$[a-z_]\w*)\b", re.IGNORECASE | re.ASCII)
_DEFAULT_MARKER = f"; {_TAG} default"
_DEFAULT_DECLARATION = re.compile(
    r"^(?:; )?(global (\$[a-z_]\w*) = -?\d+)$",
    re.IGNORECASE | re.ASCII,
)
_LEGACY_FALLBACK = re.compile(
    r"^(?:; )?(global (\$[a-z_]\w*) = -?\d+) ; SSMT bone matrix default$",
    re.IGNORECASE | re.ASCII,
)
_PALETTE = r"ResourceZZ(?:MergedSkeleton|PaletteConv|RedirectPalette)_\w+"
_MATRIX_ROWS = 3
_RESOURCE = "ResourceSSMTBoneMatrix"
_NATIVE_SLOT_COUNT = re.compile(r"(?m)^[ \t]*" + re.escape(NATIVE_SLOT_COUNT_MARKER) + r"([1-9]\d*)[ \t]*\r?$")


@dataclass(frozen=True)
class BoneMatrixClip:
    slots: tuple[int, ...]
    anchor: int
    first_frame: int
    frame_count: int
    matrices: bytes
    pivot: tuple[float, float, float] | None = None
    parents: tuple[int, ...] | None = None
    manual_parents: tuple[int, ...] = ()


def attachment_slots(slots, direct_parents, root):
    """Select the nearest live parent above each connected authored chain."""
    authored = set(slots)
    result = []
    for slot in slots:
        visited = {slot}
        parent = direct_parents.get(slot, -1)
        while parent in authored and parent not in visited:
            visited.add(parent)
            parent = direct_parents.get(parent, -1)
        result.append(parent if parent >= 0 and parent not in visited else root)
    return tuple(result)


def variable_name(value: str) -> str:
    name = value.strip().removeprefix("$")
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
        raise ValueError(f"无效的骨骼矩阵变量名：{value}")
    return "$" + name


def snapshot_settings(variable, key, play=None, frame=None):
    variable = variable_name(variable)
    if variable.casefold() in {variable_name(name).casefold() for name in (play, frame) if name is not None}:
        raise ValueError("定身变量需要与播放开关和播放变量使用不同的名称")
    if variable.casefold().startswith("$ssmt_bm_cb1_"):
        raise ValueError("定身变量名不能使用导出器内部前缀 $ssmt_bm_cb1_")
    return CB1Snapshot(variable, hotkey_value(key))


def bone_order(slots, hierarchy):
    """Visit authored parents before their children and reject cycles."""
    authored = set(slots)
    order = []
    finished = set()
    for slot in sorted(authored):
        wait = []
        pending = set()
        parent = slot
        while parent in authored and parent not in finished:
            if parent in pending:
                raise ValueError(f"骨骼父子关系存在循环，涉及编号 {parent}")
            wait.append(parent)
            pending.add(parent)
            parent = hierarchy.get(parent, -1)
        order.extend(reversed(wait))
        finished.update(wait)
    return order


def merge_pose_clips(clips, hierarchy, source_hierarchy, manual_parents=()):
    """Compose edited parent chains in global bind space, then pick live anchors."""
    if not clips:
        raise ValueError("骨骼矩阵控制至少需要一副动画骨架")
    first = clips[0]
    if any((clip.first_frame, clip.frame_count) != (first.first_frame, first.frame_count) for clip in clips):
        raise ValueError("多个骨架需要使用相同的采样帧范围")
    slots = tuple(sorted(slot for clip in clips for slot in clip.slots))
    if len(slots) != len(set(slots)):
        raise ValueError("多个骨架替换了同一个全局骨骼编号")
    by_slot = {
        slot: frame
        for clip in clips
        for slot, frame in zip(clip.slots, np.frombuffer(clip.matrices, "<f4").reshape(
            clip.frame_count, len(clip.slots), 3, 4,
        ).transpose(1, 0, 2, 3))
    }
    rebased = {}
    for slot in bone_order(slots, hierarchy):
        source_parent = source_hierarchy.get(slot, -1)
        parent = hierarchy.get(slot, -1)
        if ((source_parent if source_parent in by_slot else -1) ==
                (parent if parent in by_slot else -1) and parent not in rebased):
            continue
        original = by_slot[slot]
        if source_parent in by_slot:
            source = by_slot[source_parent]
            rhs = original.copy()
            rhs[:, :, 3] -= source[:, :, 3]
            try:
                local = np.linalg.solve(source[:, :, :3], rhs)
            except np.linalg.LinAlgError as error:
                raise ValueError(f"编号 {source_parent} 的父骨矩阵不可逆，无法重新挂接编号 {slot}") from error
        else:
            local = original

        if parent in by_slot:
            parent_pose = rebased.get(parent, by_slot[parent])
            local = parent_pose[:, :, :3] @ local
            local[:, :, 3] += parent_pose[:, :, 3]
        rebased[slot] = local

    reference = next((clip for clip in clips if clip.pivot is not None and clip.anchor in slots), first)
    matrices = np.stack([rebased.get(slot, by_slot[slot]) for slot in slots], axis=1).astype("<f4", copy=False)
    return BoneMatrixClip(
        slots, reference.anchor, first.first_frame, first.frame_count, matrices.tobytes(), reference.pivot,
        attachment_slots(slots, hierarchy, reference.anchor), tuple(manual_parents),
    )


def reconcile_bone_matrix_defaults(text: str) -> str:
    """Use a fallback declaration only while no other section owns that variable."""
    if _DEFAULT_MARKER not in text:
        return text
    lines = text.splitlines(keepends=True)
    fallbacks = {}
    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        if fallback := _LEGACY_FALLBACK.fullmatch(bare):
            fallbacks[i] = (fallback[1], fallback[2], True)
        elif i and lines[i - 1].strip() == _DEFAULT_MARKER:
            if fallback := _DEFAULT_DECLARATION.fullmatch(bare):
                fallbacks[i] = (fallback[1], fallback[2], False)
    declared = {
        match[1].casefold()
        for i, line in enumerate(lines)
        if i not in fallbacks and (match := _GLOBAL.match(line))
    }
    for i, (declaration, variable, legacy) in fallbacks.items():
        ending = lines[i][len(lines[i].rstrip("\r\n")):]
        prefix = "; " if variable.casefold() in declared else ""
        marker = _DEFAULT_MARKER + (ending or "\n") if legacy else ""
        lines[i] = marker + prefix + declaration + ending
    return "".join(lines)


def _palette_capacities(text: str) -> dict[str, int]:
    capacities = {}
    current = ""
    for line in text.splitlines():
        header = _SECTION.fullmatch(line)
        if header:
            current = header[1]
        elif re.fullmatch(_PALETTE, current, re.IGNORECASE):
            size = re.fullmatch(r"\s*array\s*=\s*(\d+)\s*(?:;.*)?", line, re.IGNORECASE)
            if size and int(size[1]) % _MATRIX_ROWS == 0:
                capacities[current.casefold()] = int(size[1]) // _MATRIX_ROWS
    return capacities


def _native_slot_count(text: str, capacities: dict[str, int]) -> int:
    # The allocated pool can include rows outside the native vertex-group range.
    allocated = max(capacities.values(), default=0)
    declared = max((int(match[1]) for match in _NATIVE_SLOT_COUNT.finditer(text)), default=allocated)
    if declared > allocated:
        raise ValueError("原生骨骼编号范围超过 ZZMI 合并骨骼缓冲容量")
    return declared


def native_palette_size(ini_path: Path) -> int:
    text = ini_path.read_text(encoding="utf-8-sig")
    return _native_slot_count(text, _palette_capacities(text))


def _restore_generated_draw(match):
    header = match[0].splitlines()[0]
    original = header.partition(f"{_BEGIN} draw|")[2]
    if original:
        indent = header[:len(header) - len(header.lstrip())]
        return indent + original + "\n"
    for target in ("blend", "hash"):
        original = header.partition(f"{_BEGIN} {target}|")[2]
        if original:
            return original + "\n"
    return ""


def _configuration_text(
    content: str, play: str | None, frame: str | None, clip: BoneMatrixClip | None,
    *, play_on_load: bool = False, initial_frame: int | None = None, support_key=None, cb1=None, layers=None,
    extra_draws=None, physics=None, unaffected_physics_draws=(),
):
    text = _BLOCK.sub(_restore_generated_draw, content).rstrip("\r\n")
    capacities = _palette_capacities(text)
    size = _native_slot_count(text, capacities)
    table = []
    defaults = [_BEGIN + " defaults"]
    if clip:
        if size == 0:
            raise ValueError("导出的 INI 没有可替换的 ZZMI 合并骨架槽")
        capacity = layers.capacity if layers else size
        if clip.slots[-1] >= capacity:
            raise ValueError("新增骨骼需要通过可挂接的动画骨架导出")
        if any(parent >= capacity for parent in clip.manual_parents):
            raise ValueError("手动父骨编号超出导出的全局顶点组范围")
        table = [0xFFFFFFFF] * capacity
        for track, slot in enumerate(clip.slots):
            if slot < capacity:
                table[slot] = track
        if layers:
            defaults.extend(layers.constants(f"; {_TAG} default"))
        else:
            starting_frame = clip.first_frame if initial_frame is None else initial_frame
            defaults.extend((
                _DEFAULT_MARKER, f"global {play} = {int(play_on_load)}",
                _DEFAULT_MARKER, f"global {frame} = {starting_frame}",
            ))
    elif cb1 is None:
        raise ValueError("需要设置骨架或 CB1 定身控制")
    if cb1:
        defaults.extend((_DEFAULT_MARKER, f"global {cb1.variable} = 0"))
    if physics:
        defaults.extend(physics.constants(_DEFAULT_MARKER))
    defaults.append(_END)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        header = _SECTION.fullmatch(line)
        if header and header[1].casefold() == "constants":
            lines[i + 1:i + 1] = defaults
            break
    else:
        lines[:0] = ["[Constants]", *defaults, ""]
    if clip:
        lines.extend((
            "", _BEGIN + " resources",
            f"[{_RESOURCE}Tracks]", "type = Buffer", "format = R32G32B32A32_FLOAT",
            "filename = res/SSMTBoneMatrixFrames.buf", "",
            f"[{_RESOURCE}SlotMap]", "type = Buffer", "format = R32_UINT",
            "filename = res/SSMTBoneMatrixSlots.buf", "",
            f"[{_RESOURCE}Parents]", "type = Buffer", "format = R32_UINT",
            "filename = res/SSMTBoneMatrixParents.buf", "",
            f"[{_RESOURCE}Clip]", "type = Buffer", "format = R32G32B32A32_UINT",
            f"data = {layers.tracks if layers else len(clip.slots)} {clip.frame_count} {clip.anchor} {size}", "",
            _END,
        ))
    patched = add_scoped_playback(
        "\n".join(lines) + "\n", play, frame, clip, capacities, size, support_key, cb1, layers, extra_draws, physics,
        unaffected_physics_draws,
    )
    return reconcile_bone_matrix_defaults(patched), struct.pack(f"<{len(table)}I", *table)


def export_cb1_snapshot(ini_path: Path, cb1: CB1Snapshot):
    """Add scoped CB1 capture without requiring an animation clip or skin assets."""
    content = _BLOCK.sub(_restore_generated_draw, ini_path.read_text(encoding="utf-8-sig"))
    capacity = _native_slot_count(content, _palette_capacities(content))
    if capacity:
        prepare_native_blends(content, ini_path.parent, capacity, None)
    text, _ = _configuration_text(content, None, None, None, cb1=cb1)
    write_bytes_if_changed_atomic(str(ini_path), text.encode("utf-8"))


def export_clip(
    ini_path: Path, play: str, frame: str, clip: BoneMatrixClip,
    *, play_on_load: bool = False, initial_frame: int | None = None, cb1=None, rig_layers=(), physics=None,
):
    content = _BLOCK.sub(_restore_generated_draw, ini_path.read_text(encoding="utf-8-sig"))
    palette_size = _native_slot_count(content, _palette_capacities(content))
    if not palette_size:
        raise ValueError("导出的 INI 没有可替换的 ZZMI 合并骨架槽")
    layers = pack_layers(rig_layers, palette_size, clip) if rig_layers else None
    if physics and (not layers or len(physics.followers) != layers.capacity):
        raise ValueError("刚体骨骼表需要与导出的分层骨骼矩阵对应")
    support_key, support = (None, b"")
    if clip.pivot is not None and clip.slots[:palette_size] == tuple(range(palette_size)):
        support_key, support = read_ground_support(content, ini_path.parent, clip.anchor, layers)
    native_blends, extra_draws, fallback_assets, unaffected = prepare_native_blends(
        content, ini_path.parent, palette_size, layers, physics,
    )
    text, slot_map = _configuration_text(
        content, play, frame, clip, play_on_load=play_on_load,
        initial_frame=initial_frame, support_key=support_key, cb1=cb1, layers=layers, extra_draws=extra_draws,
        physics=physics, unaffected_physics_draws=unaffected,
    )
    if native_blends:
        text = bind_native_blends(text, native_blends)
    resources = ini_path.parent / "res"
    resources.mkdir(exist_ok=True)
    toolset = Path(__file__).resolve().parents[1] / "Toolset"
    full = clip.pivot is not None and clip.slots == tuple(range(palette_size))
    parents = clip.parents if clip.parents is not None else (clip.anchor,) * len(clip.slots)
    if len(parents) != len(clip.slots):
        raise ValueError("骨骼矩阵控制的父骨索引数与动画轨道数不一致")
    parents = tuple(parent if not full and 0 <= parent < palette_size else 0xFFFFFFFF for parent in parents)
    assets = [
        (SKIN_SHADER, (toolset / SKIN_SHADER).read_bytes()),
        ("SSMTBoneMatrixSlots.buf", slot_map),
        ("SSMTBoneMatrixParents.buf", struct.pack(f"<{len(parents)}I", *parents)),
    ]
    assets.extend(layers.assets if layers else (("SSMTBoneMatrixFrames.buf", clip.matrices),))
    assets.extend(fallback_assets)
    if physics:
        physics_assets = physics.assets()
        assets.extend(physics_assets)
        assets.append((PHYSICS_SHADER, solver_shader_source((toolset / PHYSICS_SHADER).read_bytes(), physics_assets)))
        assets.extend((name, (toolset / name).read_bytes()) for name in (PREVIEW_SHADER, PREVIEW_PIXEL_SHADER))
    if clip.pivot is not None and f"cs = res/{RENDER_SHADER}" in text:
        assets.extend((
            (RENDER_SHADER, (toolset / RENDER_SHADER).read_bytes()),
            ("SSMTBoneMatrixBindPivot.buf", struct.pack("<4f", *clip.pivot, 1.0)),
            ("SSMTBoneMatrixReferenceBasis.buf", bytes(6 * 16)),
            ("SSMTBoneMatrixWorldInit.buf", bytes(4 * 16)),
        ))
        if support:
            assets.append((SUPPORT_FILE, support))
    for target, payload in assets:
        write_bytes_if_changed_atomic(str(resources / target), payload)
    write_bytes_if_changed_atomic(str(ini_path), text.encode("utf-8"))
