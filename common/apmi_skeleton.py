import hashlib
import json
import re
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

import numpy

SKELETON_PROPERTY = "3DMigoto:APMISkeleton"
REPLACED_PARTS_PROPERTY = "3DMigoto:APMIReplacedParts"
MATRIX_STRIDE = 64
BLEND_DTYPE = numpy.dtype([("weight", "<f4", (4,)), ("index", "<u4", (4,))])


@dataclass(frozen=True)
class PaletteOrder:
    blend_hash: str
    local_indices: tuple[int, ...]


@dataclass(frozen=True)
class Palette:
    output: str
    ordinal: int
    shader: str
    pose_constants: bytes
    palette_hash: str
    count: int
    start: int
    slots: tuple[int, ...] = ()
    writes: tuple[int, ...] = ()
    blend_hash: str = ""
    orders: tuple[PaletteOrder, ...] = ()


@dataclass(frozen=True)
class Skeleton:
    identity: str
    palettes: tuple[Palette, ...]
    count: int


def capture_folder(workspace):
    from .efmi_skeleton import EFMISkeletonMergeHelper

    folder = EFMISkeletonMergeHelper.resolve_frame_analysis_dir(str(workspace))
    if not folder or not (Path(folder) / "log.txt").is_file():
        raise ValueError("APMI：请关联提取模型时的 FrameAnalysis 目录。")
    return Path(folder)


def selected_metadata(workspace):
    workspace = Path(workspace)
    selection = json.loads((workspace / "Import.json").read_text(encoding="utf-8-sig"))
    for key, game_type in selection.items():
        lod, _, part = key.rpartition(".")
        yield key, workspace / lod / part / ("TYPE_" + game_type) / (part + ".json")


def _unique_matrices(data):
    rows = Counter(struct.iter_unpack("<16f", data))
    identity = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)
    return {matrix for matrix, count in rows.items() if count == 1 and matrix != identity}


def _matrix_rows(data, label):
    if not data or len(data) % MATRIX_STRIDE:
        raise ValueError(f"APMI：{label} 的 cs-t2 不是 64 字节矩阵数组。")
    rows = numpy.frombuffer(data, dtype="<f4").reshape(-1, 16)
    if not numpy.isfinite(rows).all() or not (rows[:, [3, 7, 11, 15]] == (0, 0, 0, 1)).all():
        raise ValueError(f"APMI：{label} 的 cs-t2 含无效仿射矩阵。")
    return tuple(struct.iter_unpack("<16f", data))


def _pose_signatures(sources, samples_folder):
    poses = {output: [_matrix_rows(data, output)] for output, (_, data) in sources.items()}
    if samples_folder is not None:
        for folder in sorted(samples_folder.glob("FrameAnalysis-*")):
            files = {output: folder / (output + ".buf") for output in sources}
            if all(path.is_file() for path in files.values()):
                for output, path in files.items():
                    rows = _matrix_rows(path.read_bytes(), path)
                    if len(rows) != sources[output][0].count:
                        raise ValueError(f"APMI：骨骼补充姿态 {path} 的槽数与原始抓帧不一致。")
                    poses[output].append(rows)
    return {output: list(zip(*frames)) for output, frames in poses.items()}


def _merge_palette_slots(sources, samples_folder):
    signatures = _pose_signatures(sources, samples_folder)
    canonical = {}
    owners = {}
    palettes = []
    next_slot = 0
    for output, (source, _) in sorted(sources.items()):
        counts = Counter(signatures[output])
        start = next_slot
        slots = []
        reordered = any(index >= 0 and index != local for order in source.orders
                        for local, index in enumerate(order.local_indices))
        for local, signature in enumerate(signatures[output]):
            # A matrix that never moves or appears twice within one palette does not identify a bone.
            shared = not reordered and counts[signature] == 1 and any(row != signature[0] for row in signature[1:])
            slot = canonical.get(signature) if shared else None
            if slot is None:
                slot = next_slot
                next_slot += 1
                if shared:
                    canonical[signature] = slot
            slots.append(slot)
            covered = all(order.local_indices[local] >= 0 for order in source.orders)
            if slot not in owners or (covered and not owners[slot][0]):
                owners[slot] = covered, output, local
        palettes.append(replace(source, start=start, slots=tuple(slots)))
    return tuple(replace(palette, writes=tuple(
        slot if owners[slot][1:] == (palette.output, local) else -1
        for local, slot in enumerate(palette.slots)
    )) for palette in palettes), next_slot


def infer_palette_order(reference, observed, count):
    if len(reference) != len(observed) or not len(reference):
        raise ValueError("蒙皮顶点数量不同，无法对应原生骨骼槽。")
    old_weights, new_weights = reference["weight"], observed["weight"]
    old_ids, new_ids = reference["index"], observed["index"]
    if (not numpy.isfinite(old_weights).all() or not numpy.isfinite(new_weights).all()
            or (old_weights < 0).any() or (new_weights < 0).any()):
        raise ValueError("蒙皮权重含无效数据。")
    old_active, new_active = old_weights > 0, new_weights > 0
    overlap = old_active & new_active
    pairs = numpy.unique(numpy.column_stack((old_ids[overlap], new_ids[overlap])), axis=0)
    if ((old_ids[old_active] >= count).any() or (new_ids[new_active] >= count).any()
            or len(numpy.unique(pairs[:, 0])) != len(pairs)
            or len(numpy.unique(pairs[:, 1])) != len(pairs)
            or len(numpy.unique(old_ids[old_active])) != len(pairs)):
        raise ValueError("Blend 缓冲无法建立完整且唯一的已用骨骼槽对应。")
    if (pairs[:, 0] != pairs[:, 1]).any() and (
        not numpy.array_equal(old_active, new_active)
        or not numpy.allclose(old_weights, new_weights, atol=1e-4, rtol=0)
    ):
        raise ValueError("Blend 骨骼槽和权重同时发生变化，无法验证重排。")
    result = [-1] * count
    for old, new in pairs:
        result[int(old)] = int(new)
    return tuple(result)


def _palette_orders(source, captures, bindings, position_stride, blend_stride):
    if not captures:
        return ()
    if blend_stride != BLEND_DTYPE.itemsize or position_stride < 12 or position_stride % 4:
        raise ValueError(f"APMI：{source.output} 的 Blend 布局不支持验证补充槽位顺序。")
    position_bytes = bindings["cs-t0"][1].read_bytes()
    blend_bytes = bindings["cs-t1"][1].read_bytes()
    reference = numpy.frombuffer(blend_bytes, dtype=BLEND_DTYPE)
    reference_xyz = numpy.frombuffer(position_bytes, "<f4").reshape(-1, position_stride // 4)[:, :3]
    orders = {}
    for position_path, blend_path in captures:
        name = re.search(r"-cs-t1=([0-9a-f]{8})(?:-|\.buf$)", blend_path.name, re.I)
        if (not name or position_path.parent != blend_path.parent
                or position_path.name.split(".", 1)[0].split("-", 1)[0] != blend_path.name.split(".", 1)[0].split("-", 1)[0]):
            raise ValueError(f"APMI：{source.output} 的补充 Position 和 Blend 必须来自同一蒙皮调用。")
        blend_hash = name[1].lower()
        if blend_hash == source.blend_hash:
            continue
        observed_xyz = numpy.fromfile(position_path, "<f4").reshape(-1, position_stride // 4)[:, :3]
        if not numpy.array_equal(reference_xyz, observed_xyz):
            raise ValueError(f"APMI：{source.output} 的补充 Position 顶点顺序与提取数据不一致。")
        observed = numpy.fromfile(blend_path, dtype=BLEND_DTYPE)
        try:
            order = infer_palette_order(reference, observed, source.count)
        except ValueError as error:
            raise ValueError(f"APMI：{source.output} 的补充 Blend {blend_hash} 无法验证。{error}") from error
        if orders.setdefault(blend_hash, order) != order:
            raise ValueError(f"APMI：Blend {blend_hash} 对应了不同的本地骨骼编号。")
    return tuple(PaletteOrder(key, indices) for key, indices in sorted(orders.items()))


def _blend_captures(workspace):
    path = Path(workspace) / "Config" / "APMIBlendLayouts.json"
    if not path.is_file():
        return {}
    captures = defaultdict(list)
    for record in json.loads(path.read_text(encoding="utf-8-sig")):
        captures[record["output"]].append((Path(workspace) / record["position"], Path(workspace) / record["blend"]))
    return captures


def _read_palette(capture, metadata, key, blend_captures):
    from ..utils.format_utils import FormatUtils

    categories = {
        category.D3D11ElementList[0].Category: category
        for category in metadata.CategoryBufferList if category.D3D11ElementList
    }
    position = categories.get("Position")
    blend = categories.get("Blend")
    if not metadata.GPU_PreSkinning or not position or not blend:
        raise ValueError(f"APMI：{key} 缺少 GPU 蒙皮的 Position 或 Blend 布局。")
    index_types = [
        numpy.dtype(FormatUtils.get_nptype_from_format(element.Format))
        for element in blend.D3D11ElementList if element.SemanticName == "BLENDINDICES"
    ]
    if not index_types or any(dtype.kind not in "iu" for dtype in index_types):
        raise ValueError(f"APMI：{key} 的骨骼索引必须使用整数 GameType 格式。")
    output = metadata.VertexLimitVB.lower()
    matches = capture.skin_passes(output, position.Stride, blend.Stride)
    if len(matches) != 1:
        raise ValueError(f"APMI：{key} 的蒙皮输出出现多次，无法确认同一角色实例。")
    ordinal, shader, _, bindings = matches[0]
    if "cs-t2" not in bindings or "cs-cb1" not in bindings:
        raise ValueError(f"APMI：{key} 的提取抓帧缺少 cs-t2 骨骼矩阵或 cs-cb1。")
    palette_hash, path = bindings["cs-t2"]
    data = path.read_bytes()
    count = len(_matrix_rows(data, key))
    cb1 = bindings["cs-cb1"][1].read_bytes()
    source = Palette(output, ordinal, shader, cb1, palette_hash, count, 0, blend_hash=bindings["cs-t1"][0])
    source = replace(source, orders=_palette_orders(source, blend_captures.get(output), bindings, position.Stride, blend.Stride))
    return source, data, min(
        numpy.iinfo(dtype).max for dtype in index_types
    )


def build_skeletons(capture, metadata_paths, workspace=None, *, use_pose_samples=True):
    """Group palettes with matching pose constants and multiple distinct shared pose matrices."""
    from .submesh_json import SubmeshJson

    lods = defaultdict(dict)
    parts = {}
    samples_folder = Path(workspace) / "Config" / "APMIPoseSamples" if workspace is not None and use_pose_samples else None
    blend_captures = _blend_captures(workspace) if workspace is not None else {}
    for key, path in sorted(metadata_paths):
        lod = key.split(".", 1)[0]
        source, data, limit = _read_palette(capture, SubmeshJson(str(path)), key, blend_captures)
        prior = lods[lod].setdefault(source.output, (source, data))
        if prior != (source, data):
            raise ValueError(f"APMI：{key} 与同一蒙皮输出的骨骼矩阵不一致。")
        parts[key] = lod, source.output, limit

    by_output = {}
    for lod, inputs in lods.items():
        anchors = {output: _unique_matrices(data) for output, (_, data) in inputs.items()}
        pending = set(inputs)
        while pending:
            connected = {min(pending)}
            pending.difference_update(connected)
            while adjacent := {
                other for other in pending
                if any(inputs[output][0].pose_constants == inputs[other][0].pose_constants
                       and len(anchors[output] & anchors[other]) >= 3 for output in connected)
            }:
                connected.update(adjacent)
                pending.difference_update(adjacent)
            palettes, count = _merge_palette_slots(
                {output: inputs[output] for output in connected}, samples_folder,
            )
            layout = [(p.output, p.palette_hash, p.count, p.shader) for p in palettes]
            if count == sum(p.count for p in palettes):
                identity = 1, lod, layout
            else:
                identity = 2, lod, layout, [p.slots for p in palettes]
            fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
            skeleton = Skeleton(fingerprint, palettes, count)
            by_output.update({(lod, p.output): (skeleton, p) for p in palettes})

    result = {}
    for key, (lod, output, limit) in parts.items():
        skeleton, palette = by_output[lod, output]
        if skeleton.count > min(int(limit) + 1, 10000):
            raise ValueError(f"APMI：{key} 的 GameType 无法容纳合并后的 {skeleton.count} 个骨骼槽。")
        result[key] = skeleton, palette
    return result
