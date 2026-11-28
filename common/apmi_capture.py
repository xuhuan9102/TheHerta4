import json
import re
import struct
from collections import defaultdict
from pathlib import Path


_SKIN_GROUP_SIZE = 64
_SKIN_DUMP = re.compile(r"(\d+)-(cs-t[012]|cs-cb[01]|u0)=([0-9a-f]{8})-cs=([0-9a-f]{16})\.buf", re.I)
_DRAW_DUMP = re.compile(r"(\d+)-(ib|vb[013])=([0-9a-f]{8})-vs=([0-9a-f]{16})-.*\.buf", re.I)
_DRAW_CALL = re.compile(r"(\d+) DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+), BaseVertexLocation:(-?\d+)\)")
_DISPATCH_CALL = re.compile(r"(\d+) Dispatch\(ThreadGroupCountX:(\d+), ThreadGroupCountY:(\d+), ThreadGroupCountZ:(\d+)\)")
_VB_ELEMENT = re.compile(
    r"\s*SemanticName: (\w+)\s+SemanticIndex: (\d+)\s+Format: (\w+)"
    r"\s+InputSlot: (\d+)\s+AlignedByteOffset: (\d+)"
)


def texcoord_layout(game_type):
    layout = {}
    for element in game_type.D3D11ElementList:
        if element.Category == "Texcoord":
            layout[element.SemanticName, element.SemanticIndex] = (
                int(element.AlignedByteOffset),
                element.Format,
            )
    return layout


def skin_group_count(vertex_count):
    return (vertex_count + _SKIN_GROUP_SIZE - 1) // _SKIN_GROUP_SIZE


def _read_texcoord_layout(path, label):
    if path is None or not path.is_file():
        raise ValueError(f"APMI：{label} 的抓帧缺少 vb1 布局，请保留 VB 的 TXT 文件。")
    with path.open(encoding="utf-8", errors="replace") as stream:
        header = []
        for line in stream:
            if line.startswith("vertex-data:"):
                break
            header.append(line)
    text = "".join(header)
    captured_stride = re.search(r"^stride: (\d+)", text, re.M)
    captured = {
        (name, int(index)): (int(offset), fmt)
        for name, index, fmt, slot, offset in _VB_ELEMENT.findall(text)
        if slot == "1"
    }
    if captured_stride is None or not captured:
        raise ValueError(f"APMI：{label} 的抓帧没有完整的 vb1 声明。")
    return int(captured_stride[1]), captured


def _validate_texcoord_layout(path, part, expected):
    captured_stride, captured = _read_texcoord_layout(path, part.unique_str)
    stride = part.d3d11_game_type.CategoryStrideDict.get("Texcoord")
    if captured_stride != stride:
        raise ValueError(f"APMI：{part.unique_str} 的 GameType 声明 vb1 步长为 {stride}，抓帧记录为 {captured_stride}。")
    for (name, index), (offset, fmt) in expected.items():
        actual = captured.get((name, index))
        if actual != (offset, fmt):
            found = f"偏移 {actual[0]} / {actual[1]}" if actual else "缺失"
            raise ValueError(
                f"APMI：{part.unique_str} 的 vb1 {name}{index} 布局不一致。"
                f"GameType 为偏移 {offset} / {fmt}，抓帧为 {found}。请校正数据类型后重新导入。"
            )
    # Multiple shader semantics can alias one physical field in the vertex buffer.
    if set(captured.values()) != set(expected.values()):
        raise ValueError(f"APMI：{part.unique_str} 的 GameType 未覆盖抓帧记录的 vb1 字段。")


def _aliases(elements):
    grouped = defaultdict(set)
    for semantic, field in elements.items():
        grouped[field].add(semantic)
    return {frozenset(semantics) for semantics in grouped.values()}


def plan_texcoord_overrides(primary, observed):
    """Rebind verified variants to the exported GameType without changing the vertex buffer."""
    by_stride = defaultdict(dict)
    covered_shaders = defaultdict(set)
    for shader, (stride, fields) in observed:
        if shader not in primary:
            continue
        target_stride, target = primary[shader]
        if fields == target and stride == target_stride:
            continue
        if stride == target_stride or _aliases(fields) != _aliases(target):
            raise ValueError("APMI：补充抓帧的 vb1 无法按步长区分，或使用了不同的语义别名。")
        for semantic, (target_offset, target_format) in target.items():
            source_format = fields[semantic][1]
            if source_format != target_format and {
                source_format, target_format
            } != {"R16G16_FLOAT", "R32G32_FLOAT"}:
                raise ValueError(f"APMI：补充抓帧的 vb1 {semantic} 格式与 GameType 不兼容。")
            binding = target_offset, target_format if source_format != target_format else None
            if by_stride[stride].setdefault(semantic, binding) != binding:
                raise ValueError(f"APMI：补充抓帧的 vb1 步长 {stride} 对应多种输入声明。")
        covered_shaders[stride].add(shader)

    result = {}
    for stride, elements in sorted(by_stride.items()):
        if covered_shaders[stride] != primary.keys():
            continue
        lines = []
        for (semantic, index), (offset, fmt) in sorted(elements.items()):
            if fmt:
                lines.append(f"vb1->ElementFormat({semantic}, {index}) = DXGI_FORMAT_{fmt}")
            lines.append(f"vb1->ElementOffset({semantic}, {index}) = {offset}")
        result[stride] = lines
    return result


def render_layout_captures(workspace):
    workspace = Path(workspace)
    profile = workspace / "Config" / "APMIRenderCaptures.json"
    if not profile.is_file():
        return []
    paths = json.loads(profile.read_text(encoding="utf-8-sig"))
    if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
        raise ValueError(f"APMI：{profile} 应保存抓帧目录列表。")
    return [APMICapture(workspace / path) for path in paths]


class APMICapture:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.skin = defaultdict(dict)
        self.draw = defaultdict(dict)
        self.texcoord_dumps = {}
        self.calls = {}
        self.dispatches = {}
        self.dump_map = {}
        self._resolved_paths = {}
        log_path = self.folder / "log.txt"
        with log_path.open(encoding="utf-8", errors="replace") as log:
            for line in log:
                match = _DRAW_CALL.match(line)
                if match:
                    ordinal, count, start, base = match.groups()
                    self.calls[ordinal] = int(count), int(start), int(base)
                match = _DISPATCH_CALL.match(line)
                if match:
                    ordinal, x, y, z = match.groups()
                    self.dispatches[ordinal] = int(x), int(y), int(z)
                if "3DMigoto Dumping Buffer " in line and " -> " in line:
                    source, destination = line.rsplit(" -> ", 1)
                    source_path = source.split("3DMigoto Dumping Buffer ", 1)[-1].strip()
                    source_name = Path(source_path).name
                    self.dump_map[source_name] = Path(destination.strip())

        logical_buffers = {path.name for path in self.folder.glob("*.buf")}
        logical_buffers.update(
            name for name in self.dump_map if name.lower().endswith(".buf")
        )
        for logical_name in sorted(logical_buffers):
            path = self._resolve_dump(logical_name)
            match = _SKIN_DUMP.fullmatch(logical_name)
            if match:
                ordinal, slot, resource_hash, shader = match.groups()
                self.skin[ordinal, shader][slot] = (resource_hash, path)
                continue
            match = _DRAW_DUMP.fullmatch(logical_name)
            if match:
                ordinal, slot, resource_hash, shader = match.groups()
                self.draw[ordinal, shader][slot] = resource_hash
                if slot == "vb1":
                    txt_name = Path(logical_name).with_suffix(".txt").name
                    self.texcoord_dumps[ordinal, shader] = self._resolve_dump(txt_name)

    def _resolve_dump(self, logical_name):
        if logical_name in self._resolved_paths:
            return self._resolved_paths[logical_name]
        candidates = []
        destination = self.dump_map.get(logical_name)
        if destination is not None:
            candidates.append(destination)
            candidates.append(self.folder / "deduped" / destination.name)
        candidates.append(self.folder / logical_name)
        existing = [path for path in candidates if path.is_file()]
        non_empty = [path for path in existing if path.stat().st_size > 0]
        resolved = (non_empty or existing or candidates)[0]
        self._resolved_paths[logical_name] = resolved
        return resolved

    def skin_passes(self, output_hash, position_stride, blend_stride):
        matches = []
        for (ordinal, shader), bindings in self.skin.items():
            if not {"u0", "cs-t0", "cs-t1", "cs-cb0"} <= bindings.keys() or bindings["u0"][0] != output_hash:
                continue
            position_size = bindings["cs-t0"][1].stat().st_size
            count, remainder = divmod(position_size, position_stride)
            cb = bindings["cs-cb0"][1].read_bytes()
            blend = bindings["cs-t1"][1]
            if (count and not remainder and len(cb) == 16 and struct.unpack_from("<I", cb)[0] == count
                    and self.dispatches.get(ordinal) == (skin_group_count(count), 1, 1)
                    and blend.stat().st_size == count * blend_stride and bindings["u0"][1].stat().st_size == position_size):
                matches.append((int(ordinal), shader, cb[4:], bindings))
        if not matches:
            raise ValueError(
                f"APMI：抓帧中没有匹配输出 {output_hash} 的蒙皮通道。"
                f"请核对 GameType 的 Position 步长 {position_stride} 和 Blend 步长 {blend_stride}。"
            )
        return matches

    def skin_parameters(self, output_hash, position_stride, blend_stride):
        params = {match[2] for match in self.skin_passes(output_hash, position_stride, blend_stride)}
        if len(params) != 1:
            raise ValueError(f"APMI：蒙皮输出 {output_hash} 在抓帧中对应多组蒙皮参数。")
        return params.pop()

    def _matching_draws(self, draw_ib, output_hash, count, start):
        for (ordinal, shader), bindings in self.draw.items():
            if (bindings.get("ib") == draw_ib and bindings.get("vb0") == output_hash
                    and self.calls.get(ordinal) == (count, start, 0)):
                yield ordinal, shader, bindings

    def native_texcoord_layouts(self, draw_ib, output_hash, count, start):
        for ordinal, shader, bindings in self._matching_draws(draw_ib, output_hash, count, start):
            if "vb1" in bindings:
                yield shader, _read_texcoord_layout(self.texcoord_dumps.get((ordinal, shader)), draw_ib)

    def draw_passes(self, draw_ib, output_hash, count, start, part, texcoord_layout):
        shaders = set()
        ordinals = []
        previous_position = False
        for ordinal, shader, bindings in self._matching_draws(draw_ib, output_hash, count, start):
            if "vb3" in bindings and bindings["vb3"] != output_hash:
                raise ValueError(f"APMI：{draw_ib} 的 vb3 与蒙皮输出不同，当前无法恢复该渲染输入。")
            _validate_texcoord_layout(self.texcoord_dumps.get((ordinal, shader)), part, texcoord_layout)
            previous_position |= "vb3" in bindings
            shaders.add(shader)
            ordinals.append(int(ordinal))
        if not shaders:
            raise ValueError(f"APMI：抓帧中没有匹配 {draw_ib}（索引数 {count}，起始索引 {start}）的 DrawIndexed。")
        return shaders, previous_position, min(ordinals)
