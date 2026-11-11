"""Optional ZZMI surface-garment export and current-frame render routing."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import numpy as np

from ..common.safe_write import write_bytes_if_changed_atomic
from .drag_cloth_geometry import bake_garments
from .drag_collision import bake_sweep_order

TOOLSET = Path(__file__).resolve().parents[1] / "Toolset" / "drag_cloth"
SHADERS = (
    "cloth_nodes.hlsl",
    "cloth_groups.hlsl",
    "apply.hlsl",
    "exterior.hlsl",
    "contacts.hlsl",
    "hull.hlsl",
    "self.hlsl",
    "select.hlsl",
)
DRAW_BEGIN = "; --- SSMT CLOTH DRAW BEGIN ---"
DRAW_END = "; --- SSMT CLOTH DRAW END ---"


def clean_tail(text):
    lines = []
    inside = False
    for line in str(text or "").splitlines(keepends=True):
        if DRAW_BEGIN in line:
            inside = True
            continue
        if DRAW_END in line:
            inside = False
            continue
        if not inside:
            lines.append(line)
    return "".join(lines)


def publish_in_tail(text, sections, components):
    """Old TTL wrappers can reside in a preserved UI tail rather than sections."""
    wrappers = {}
    for comp in components:
        if not comp.get("cloth_publish"):
            continue
        for section in component_sections(comp):
            for line in sections.get(section, []):
                match = re.match(
                    r"\s*run\s*=\s*(CommandListSSMTTTLDraw_\S+)", line, re.IGNORECASE
                )
                if match:
                    wrappers[match.group(1).casefold()] = comp["cloth_publish"]
    result = []
    current = ""
    for line in clean_tail(text).splitlines(keepends=True):
        if line.strip().startswith("["):
            current = line.strip().strip("[]").casefold()
        if current in wrappers and re.match(
            r"\s*run\s*=\s*CommandList\\TTL\\Draw\b", line, re.IGNORECASE
        ):
            indent = line[: len(line) - len(line.lstrip())]
            result.extend(
                [
                    indent + DRAW_BEGIN + "\n",
                    indent + "run = " + wrappers[current] + "\n",
                    indent + DRAW_END + "\n",
                ]
            )
        result.append(line)
    return "".join(result)


def _token(value):
    return re.sub(r"[^\w]+", "_", str(value)).strip("_") or "cloth"


def _field(lines, key):
    for line in lines:
        match = re.match(
            r"\s*" + re.escape(key) + r"\s*=\s*(.*?)\s*$", line, re.IGNORECASE
        )
        if match:
            return match.group(1)
    return None


def _resource_reference(value):
    return re.sub(r"^ref\s+", "", value, flags=re.IGNORECASE).split()[0]


def _skin_writes(sections, comp, group, family, vertex_count):
    """Identify writes of this component's skinned rows and their actual palette."""
    base = comp["base_resource"].casefold()
    outputs = {f"{family}_G{group}_s{slot}".casefold(): slot for slot in (1, 2)}
    palettes = re.compile(
        rf"ResourceZZ(?:MergedSkeleton_G{group}|PaletteConv_C\d+)_s([12])",
        re.IGNORECASE,
    )
    writes = []
    for section, lines in sections.items():
        if section.casefold().startswith("[customshader"):
            shader = (_field(lines, "cs") or "").replace("\\", "/").rsplit("/", 1)[-1]
            if shader.casefold() != "zzmi_merged_skin.hlsl":
                continue
            vertices = _resource_reference(_field(lines, "cs-u0") or "null")
            slot = outputs.get(vertices.casefold())
            position = _resource_reference(_field(lines, "cs-t0") or "null")
            if (
                slot is None
                or position.casefold() != base
                or _field(lines, "x1") != str(vertex_count)
            ):
                continue
            palette = _resource_reference(_field(lines, "cs-t2") or "null")
            writes.extend(
                (section, index, slot, palette, vertices)
                for index, line in enumerate(lines)
                if re.match(r"\s*dispatch\s*=", line, re.IGNORECASE)
            )
        elif family == "ResourceZZRedirectSO" and section.casefold().startswith(
            "[textureoverride_vb_"
        ):
            # Each guarded replay sets its IA/SO bindings in its own branch.
            # Restore the surrounding bindings when moving to the next branch.
            bindings = [{}]
            for index, line in enumerate(lines):
                text = line.split(";", 1)[0].strip()
                lower = text.casefold()
                if lower.startswith("if "):
                    bindings.append(bindings[-1].copy())
                    continue
                if len(bindings) > 1:
                    if lower == "endif":
                        bindings.pop()
                        continue
                    if lower == "else" or lower.startswith("elif "):
                        bindings[-1] = bindings[-2].copy()
                        continue
                key, _, value = text.partition("=")
                key = key.strip().casefold()
                current = bindings[-1]
                if key in ("vb0", "vs-t0", "so0") and value.strip():
                    current[key] = _resource_reference(value.strip())
                elif key == "draw" and current.get("vb0", "").casefold() == base:
                    # A redirect can first draw only its three prefix rows with
                    # the same VB bound. That draw did not skin the garment.
                    draw = re.fullmatch(r"\s*(\d+)\s*,\s*0\s*", value)
                    if not draw or int(draw[1]) != vertex_count:
                        continue
                    palette = current.get("vs-t0", "")
                    match = palettes.fullmatch(palette)
                    if match:
                        slot = int(match[1])
                        explicit_so = current.get("so0")
                        if (
                            explicit_so is None
                            or outputs.get(explicit_so.casefold()) == slot
                        ):
                            writes.append((section, index, slot, palette, "so0"))
    return writes


def _palette_sizes(sections, writes):
    rows = {}
    for _, _, slot, palette, _ in writes:
        declaration = sections.get(f"[{palette}]", ())
        size = int(_field(declaration, "array") or 0)
        if (
            (_field(declaration, "format") or "").upper() != "R32G32B32A32_FLOAT"
            or size <= 0
            or size % 3
            or rows.get(slot, size) != size
        ):
            raise ValueError(f"上下拖拽：蒙皮骨架 {palette} 的矩阵布局不兼容")
        rows[slot] = size
    if set(rows) != {1, 2}:
        raise ValueError("上下拖拽：找不到两个槽位的实际蒙皮写出点，无法配对顶点与骨架")
    return rows


def component_sections(comp):
    names = []
    for part in comp["parts"]:
        names.append(part["section"])
        names.extend(
            record["section"]
            for record in part.get("draw_records", ())
            if record.get("section")
        )
    return list(dict.fromkeys(names))


def clean_sections(sections, ns):
    """Remove only our inline routing and globals; normal draw code is retained."""
    prefix = "$ssmtcloth_" + ns + "_"
    for name, lines in list(sections.items()):
        cleaned = []
        inside = False
        for line in lines:
            if DRAW_BEGIN in line:
                inside = True
                continue
            if DRAW_END in line:
                inside = False
                continue
            if inside:
                continue
            if (
                name == "[Constants]"
                and prefix in line
                and line.strip().startswith("global ")
            ):
                continue
            line = re.sub(
                r" && \$ssmtcloth_" + re.escape(ns) + r"_enabled == 0", "", line
            )
            cleaned.append(line)
        sections[name] = cleaned


def stabilize_globals(sections, ns):
    prefix = "$ssmtcloth_" + ns + "_"
    lines = sections.get("[Constants]", [])
    owned = [
        line for line in lines if line.strip().startswith("global ") and prefix in line
    ]
    retained = [line for line in lines if line not in owned]
    while retained and not retained[-1].strip():
        retained.pop()
    sections["[Constants]"] = retained + [""] + sorted(owned)


class _Assets:
    def __init__(self, root, token, ns):
        self.mod = Path(root).resolve()
        self.directory = self.mod / "res" / "drag_cloth" / token
        self.directory.mkdir(parents=True, exist_ok=True)
        self.files = {}
        self.ns = ns
        self.manifest = self.directory / "manifest.json"
        self.old = (
            json.loads(self.manifest.read_text("utf8"))
            if self.manifest.exists()
            else {}
        )

    def put(self, name, data):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        blob = (
            data.encode("utf8")
            if isinstance(data, str)
            else (data.tobytes() if isinstance(data, np.ndarray) else data)
        )
        write_bytes_if_changed_atomic(str(path), blob)
        self.files[name.replace("\\", "/")] = hashlib.sha256(blob).hexdigest()
        return path.relative_to(self.mod).as_posix()

    def finish(self):
        _remove_owned(self.directory, self.old.get("files", {}), set(self.files))
        text = json.dumps(
            {"namespace": self.ns, "files": self.files},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        write_bytes_if_changed_atomic(str(self.manifest), text.encode("utf8"))


def _remove_owned(directory, files, keep=()):
    directory = Path(directory).resolve()
    for name, digest in files.items():
        if name in keep:
            continue
        target = (directory / name).resolve()
        if not target.is_relative_to(directory):
            raise ValueError("Cloth manifest path escapes its directory")
        if (
            target.is_file()
            and hashlib.sha256(target.read_bytes()).hexdigest() == digest
        ):
            target.unlink()
            if target.suffix == ".hlsl":
                for cache in target.parent.glob(target.stem + ".cs_5_0.*.bin"):
                    cache.unlink()
    for folder in sorted(
        (p for p in directory.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(folder.iterdir()):
            folder.rmdir()


def prune_assets(root, namespace, active_tokens):
    directory = Path(root) / "res" / "drag_cloth"
    if not directory.is_dir():
        return
    for manifest in directory.glob("*/manifest.json"):
        data = json.loads(manifest.read_text("utf8"))
        if data.get("namespace") != namespace or manifest.parent.name in active_tokens:
            continue
        _remove_owned(manifest.parent, data.get("files", {}))
        manifest.unlink()
        if not any(manifest.parent.iterdir()):
            manifest.parent.rmdir()
    if not any(directory.iterdir()):
        directory.rmdir()


def _shader(node, name, constants):
    struct = node._get_vertex_struct_definition().replace("VertexAttributes", "Vertex")
    for field in ("position", "normal", "tangent"):
        struct = re.sub(r"\b" + field + r"\b", field, struct, flags=re.IGNORECASE)
    common = (TOOLSET / "common.hlsli").read_text("utf8")
    common = re.sub(
        r"struct Vertex\s*\{[^}]*\};",
        lambda _: struct,
        common,
        count=1,
        flags=re.DOTALL,
    )
    text = (TOOLSET / name).read_text("utf8")
    text = text.replace('#include "constants.hlsli"', constants).replace(
        '#include "common.hlsli"', common
    )
    if name == "select.hlsl":
        text = (
            constants
            + "\n"
            + re.sub(
                r"struct Vertex\s*\{[^}]*\};",
                lambda _: struct,
                text,
                count=1,
                flags=re.DOTALL,
            )
        )
    text = re.sub(r"\bstride\s*==\s*40\b", "stride==VERTEX_STRIDE", text)
    text = re.sub(r"\bbindStride\s*==\s*40\b", "bindStride==VERTEX_STRIDE", text)
    text = re.sub(r"\bstride\s*!=\s*40\b", "stride!=VERTEX_STRIDE", text)
    text = re.sub(r"\bnativeStride\s*!=\s*40\b", "nativeStride!=VERTEX_STRIDE", text)
    return text


def bake_component(node, root, sections, comp, ns, cloth_names, body_names, name_key):
    """Bake current exported geometry, never a character-specific pose table."""
    if not cloth_names:
        return None
    positions = node._read_position_buf(root, sections, comp, comp["vertex_count"])
    normals = node._read_vertex_normals(root, sections, comp)
    if positions is None or normals is None:
        raise ValueError("上下拖拽：需要可读取的位置与法线缓冲")
    topology = node._read_component_triangles(root, sections, comp, len(positions))
    if topology is None:
        raise ValueError("上下拖拽：需要带物体注释的三角形索引")
    triangles, names = topology
    names = np.array([name_key(name) for name in names], object)
    cloth = np.isin(names, list(cloth_names))
    body = np.isin(names, list(body_names))
    if not cloth.any():
        return None
    if not body.any():
        raise ValueError(
            f"上下拖拽：{comp['comp_name']} 没有命中身体碰撞体，请使用同一合并载体内的物体"
        )
    stride, pos_offset, pos_type = node._get_vertex_attribute_layout("position")
    _, normal_offset, normal_type = node._get_vertex_attribute_layout("normal")
    _, tangent_offset, tangent_type = node._get_vertex_attribute_layout("tangent")
    if (
        stride % 4
        or pos_type != "float3"
        or normal_type != "float3"
        or tangent_type != "float4"
    ):
        raise ValueError(
            "上下拖拽：需要 float3 position/normal 和 float4 tangent，且顶点步长须按4字节对齐"
        )
    if re.search(
        r"\b(?:half|double)[1-4]?\s+\w+", node._get_vertex_struct_definition()
    ):
        raise ValueError(
            "上下拖拽：结构化顶点读取目前需要32位属性，不支持half/double混合布局"
        )
    for section_name, lines in sections.items():
        if (
            section_name.startswith(f'[TextureOverride_{comp["hash"]}_')
            and "VertexLimitRaise" in section_name
        ):
            native_stride = _field(lines, "override_byte_stride")
            if native_stride is not None and int(native_stride) != stride:
                raise ValueError(
                    "上下拖拽：原生蒙皮输出步长与顶点结构不一致，不能安全发布整个人物的VB0"
                )
    group, occ, reason = node._merged_skeleton_binding(sections, comp)
    if group is None or occ is None:
        raise ValueError("上下拖拽需要可确定的 ZZMI 合并骨架：" + reason)
    candidates = []
    for family in ("ResourceZZRedirectSO", "ResourceZZSkinOut"):
        if all(f"[{family}_G{group}_s{slot}]" in sections for slot in (1, 2)):
            candidates.append(family)
    if not candidates:
        raise ValueError("上下拖拽：产物没有当前帧合并蒙皮输出，请重新导出合并骨架")
    source = candidates[0]
    pending = component_sections(comp)
    visited = set()
    while pending:
        section = pending.pop()
        if section in visited:
            continue
        visited.add(section)
        for line in sections.get(section, ()):
            if re.search(
                r"vb0\s*=\s*(?:ref\s+)?ResourceZZSkinOut_G" + str(group) + r"_s[12]",
                line,
            ):
                source = "ResourceZZSkinOut"
            match = re.match(r"\s*run\s*=\s*(\S+)", line)
            if match and "[" + match.group(1) + "]" in sections:
                pending.append("[" + match.group(1) + "]")
    if source not in candidates:
        raise ValueError("上下拖拽：当前绘制的蒙皮输出没有完整的两个实例槽位")
    for slot in (1, 2):
        declared_stride = _field(sections[f"[{source}_G{group}_s{slot}]"], "stride")
        if declared_stride is not None and int(declared_stride) != stride:
            raise ValueError("上下拖拽：蒙皮输出视图步长与当前顶点布局不一致")
    skin_writes = _skin_writes(sections, comp, group, source, len(positions))
    palette_rows = _palette_sizes(sections, skin_writes)
    base, count = node._runtime_vertex_layout(comp, len(positions))
    _, unique = np.unique(np.sort(triangles[body], axis=1), axis=0, return_index=True)
    body_faces = triangles[body][np.sort(unique)]
    garments = bake_garments(
        positions,
        normals,
        triangles[cloth],
        body_faces,
        count,
        base,
        budget=getattr(node, "cloth_proxy_budget", 2450),
        up_axis=getattr(node, "cloth_up_axis", "AUTO"),
    )
    order, leaves = bake_sweep_order(positions[body_faces])
    ini_token = comp.get("cloth_ini_token", "mod")
    token = _token(ini_token + "_" + comp["comp_name"] + "_" + ns)
    assets = _Assets(root, token, ns)
    relative = assets.directory.relative_to(assets.mod).as_posix()
    raw_path = _field(sections[f"[{comp['base_resource']}]"], "filename")
    raw = np.fromfile(Path(root) / raw_path.replace("\\", "/"), np.uint8)
    if raw.size != len(positions) * stride:
        raise ValueError("上下拖拽：Position 的导出步长与顶点结构定义不一致")
    bind = np.zeros((count, stride), np.uint8)
    bind[base : base + len(positions)] = raw.reshape(-1, stride)
    blend_path = (
        Path(root)
        / node._buffer_dir(sections, comp)
        / (comp["base_name"] + "-Blend.buf")
    )
    blend = np.fromfile(blend_path, np.uint8)
    if blend.size != len(positions) * 32:
        raise ValueError("上下拖拽：需要四权重/四uint索引的32字节合并 Blend 缓冲")
    weights = blend.view("<f4").reshape(-1, 8)[:, :4]
    indices = blend.view("<u4").reshape(-1, 8)[:, 4:]
    selected_rows = np.unique(np.r_[body_faces.ravel(), triangles[cloth].ravel()])
    if np.any(
        indices[selected_rows][weights[selected_rows] > 0]
        >= min(palette_rows.values()) // 3
    ):
        raise ValueError("上下拖拽：衣物或身体引用了合并骨架池之外的骨骼索引")
    full_blend = np.zeros((count, 32), np.uint8)
    full_blend[base : base + len(positions)] = blend.reshape(-1, 32)
    paths = {
        name: assets.put(name + ".buf", value)
        for name, value in [
            ("bind", bind),
            ("blend", full_blend),
            ("body_vertices", (body_faces + base).astype(np.uint32)),
            ("body_order", order),
        ]
    }
    vertex_struct = node._get_vertex_struct_definition()
    bounds = (
        Path(node._get_toolset_dir()) / "rzm_drag_collision_bounds.hlsl"
    ).read_text("utf8")
    bounds = re.sub(
        r"struct VertexAttributes\s*\{[^}]*\};",
        lambda _: vertex_struct,
        bounds,
        count=1,
        flags=re.DOTALL,
    )
    paths["bounds_shader"] = assets.put("bounds.hlsl", bounds)
    if base or count != len(positions):
        constants = "\n".join(
            f"#define {key} {value}"
            for key, value in [
                ("VERTEX_STRIDE", stride),
                ("BIND_VERTEX_BASE", base),
                ("BIND_SOURCE_COUNT", len(positions)),
                ("BIND_RUNTIME_COUNT", count),
            ]
        )
        paths["copy_bind_shader"] = assets.put(
            "copy_bind.hlsl", _shader(node, "copy_bind.hlsl", constants)
        )
    for index, data in enumerate(garments):
        cfg = data["config"]
        p = int(cfg[0, 0])
        h = int(cfg[1, 3])
        ec = int(cfg[1, 1])
        fc = int(cfg[1, 2])
        cfg[4, 0] = float(getattr(node, "cloth_min_height_ratio", 0.22))
        owner = int(cfg[12, 3])
        selected = (
            data["body_regions"] == owner
            if owner
            else np.ones(len(body_faces), dtype=bool)
        )
        ranges = (positions[body_faces[selected]] - cfg[8, :3]) @ cfg[6, :3]
        if not len(ranges):
            raise ValueError("上下拖拽：没有属于这条衣物的身体碰撞表面")
        intervals = sorted(zip(ranges.min(1), ranges.max(1)))
        minimum = float(cfg[6, 3] * cfg[4, 0])
        maximum = float(cfg[6, 3])
        reached = minimum
        tolerance = float(cfg[6, 3]) * 1e-5
        for start, end in intervals:
            if end < reached:
                continue
            if start > reached + tolerance:
                break
            reached = max(reached, float(end))
        if reached < maximum - tolerance:
            raise ValueError(
                "上下拖拽：身体碰撞体未连续覆盖袜口到最低位置，请扩大身体选择或提高最低袜口高度比例"
            )
        constants = "\n".join(
            f"#define {name} {value}"
            for name, value in [
                ("PARTICLE_COUNT", p),
                ("HEM_COUNT", h),
                ("EDGE_COLORS", ec),
                ("FACE_COLORS", fc),
                ("HASH_SIZE", 4096),
                ("THREADS", 256),
                ("VERTEX_STRIDE", stride),
                ("VERTEX_FLOATS", stride // 4),
                ("POSITION_OFFSET", pos_offset // 4),
                ("NORMAL_OFFSET", normal_offset // 4),
                ("TANGENT_OFFSET", tangent_offset // 4),
            ]
        )
        for name, value in data.items():
            assets.put(f"g{index}/{name}.buf", value)
        for name in SHADERS:
            assets.put(f"g{index}/{name}", _shader(node, name, constants))
    assets.finish()
    return {
        "token": token,
        "directory": relative,
        "paths": paths,
        "garments": garments,
        "leaves": leaves,
        "body_count": len(body_faces),
        "vertices": count,
        "stride": stride,
        "vertex_base": base,
        "source_vertices": len(positions),
        "group": group,
        "occ": occ,
        "native_family": source,
        "skin_writes": skin_writes,
        "palette_rows": palette_rows,
        "dynamic_bind": (
            comp["base_resource"] if base == 0 and len(positions) == count else None
        ),
    }


class _Emitter:
    def __init__(self, node, sections, comp, ns):
        self.node = node
        self.sections = sections
        self.comp = comp
        self.ns = ns
        self.meta = comp["cloth"]
        self.suffix = comp["comp_name"] + "_" + ns
        self.prefix = "ResourceSSMTCloth_" + self.suffix + "_"
        self.var = "$ssmtcloth_" + ns + "_" + comp["comp_name"] + "_"
        self.enabled = "$ssmtcloth_" + ns + "_enabled"
        self.reset = "$ssmtcloth_" + ns + "_reset"
        self.mode = node._runtime_variable_names(ns)[0]

    def r(self, name):
        return self.prefix + name

    def s(self, name, lines):
        self.sections["[" + name + "]"] = lines

    def cmd(self, name):
        return "CommandListSSMTCloth" + name + "_" + self.suffix

    def shader(self, name):
        return "CustomShaderSSMTCloth" + name + "_" + self.suffix

    def global_(self, name, value):
        line = "global " + self.var + name + " = " + str(value)
        if line not in self.sections["[Constants]"]:
            self.sections["[Constants]"].append(line)

    def resource(
        self,
        name,
        count=None,
        fmt=None,
        stride=None,
        file=None,
        writable=False,
        vertex=False,
        alias=False,
    ):
        kind = (
            "Buffer"
            if alias
            else (
                "RWStructuredBuffer"
                if writable and stride and not vertex
                else (
                    "StructuredBuffer"
                    if stride and not vertex
                    else ("RWBuffer" if writable and not vertex else "Buffer")
                )
            )
        )
        lines = ["type = " + kind]
        if stride:
            lines += ["stride = " + str(stride)]
        if fmt:
            lines += ["format = " + fmt]
        if count is not None:
            lines += ["array = " + str(count)]
        if file:
            lines += ["filename = " + file]
        if vertex:
            lines += ["bind_flags = unordered_access vertex_buffer"]
        elif writable:
            lines += ["bind_flags = shader_resource unordered_access"]
        self.s(self.r(name), lines)

    def parameters(self, slot, phase):
        v = "$ssmtcloth_" + self.ns + "_"
        return [
            "x115 = 1",
            f'y115 = {self.meta["leaves"]}',
            f"x116 = {phase}",
            f"y116 = {self.enabled}",
            f"z116 = {self.reset}",
            "w116 = $cursorY",
            "x117 = $isMouseButtonDown",
            f"y117 = $screenH / {v}drag_sensitivity",
            f"z117 = {self.var}dt",
            f"w118 = {slot}",
            f"x119 = {self.var}micro",
            f"x121 = {v}stretch_compliance",
            f"y121 = {v}bend_compliance",
            f"z121 = {v}velocity_retention",
            f"w121 = {v}bend_min_ratio",
            f"x122 = {v}fold_height",
            f"y122 = {v}fold_spacing",
            f"z122 = {v}gather_strength",
            "w122 = 1",
        ]

    def bindings(self, g, slot):
        r = lambda name: self.r(f"G{g}_" + name)
        values = {
            2: self.r("SkinPalette_s1"),
            3: self.r("SkinPalette_s2"),
            24: self.r("NativeInput"),
            67: self.r("DetectInput"),
            75: r("Rest"),
            79: r("Faces"),
            80: r("FaceAdjacency"),
            81: r("DisplayTriangles"),
            82: r("Config"),
            83: r("DisplayWeights"),
            85: r("Representatives"),
            86: self.r("Blend"),
            87: self.r("BodyVerts"),
            89: self.r("BodyOrder"),
            91: self.r(f"Bounds_s{slot}"),
            94: r("Map"),
            96: r("Decision"),
            100: r("FaceColors"),
            105: r("BodyAdjacency"),
            106: r(f"Exterior_s{slot}"),
            107: r("HemLines"),
            108: r("SurfaceMask"),
            109: r("BodyOwners"),
            111: self.r("BindInput"),
            112: r(f"BodyContacts_s{slot}"),
            113: r("Edges"),
            114: r("EdgeOffsets"),
            115: r("FaceOffsets"),
            116: r("HemMap"),
            117: r("HemParams"),
            118: r(f"HullPoints_s{slot}"),
            119: r(f"HullCount_s{slot}"),
        }
        return [f"cs-t{i} = ref {name}" for i, name in values.items()]

    @staticmethod
    def clear_uavs():
        return [f"cs-u{i} = null" for i in range(8)]

    def garment(self, g, data):
        cfg = data["config"]
        p = int(cfg[0, 0])
        edges = int(cfg[1, 0])
        body = self.meta["body_count"]
        path = self.meta["directory"] + f"/g{g}"
        r = lambda name: self.r(f"G{g}_" + name)
        static = {
            "Rest": ("rest", "R32G32B32A32_FLOAT"),
            "Map": ("map", "R32_UINT"),
            "Faces": ("faces", "R32_UINT"),
            "FaceAdjacency": ("face_adjacency", "R32_UINT"),
            "Config": ("config", "R32G32B32A32_FLOAT"),
            "Representatives": ("representatives", "R32_UINT"),
            "Edges": ("edges", "R32G32B32A32_FLOAT"),
            "EdgeOffsets": ("edge_offsets", "R32_UINT"),
            "FaceColors": ("face_colors", "R32_UINT"),
            "FaceOffsets": ("face_offsets", "R32_UINT"),
            "HemMap": ("hem_map", "R32_UINT"),
            "HemParams": ("hem_params", "R32G32B32A32_FLOAT"),
            "HemLines": ("hem_lines", "R32G32B32A32_FLOAT"),
            "BodyAdjacency": ("body_adjacency", "R32_UINT"),
            "SurfaceMask": ("surface_mask", "R32_UINT"),
            "BodyOwners": ("body_regions", "R32_UINT"),
            "DisplayTriangles": ("display_triangles", "R32_UINT"),
            "DisplayWeights": ("display_weights", "R32G32B32A32_FLOAT"),
        }
        for name, (file, fmt) in static.items():
            self.resource(f"G{g}_" + name, fmt=fmt, file=path + "/" + file + ".buf")
        self.resource(f"G{g}_State", 24, "R32G32B32A32_FLOAT", writable=True)
        self.resource(f"G{g}_Decision", 3, "R32G32B32A32_FLOAT", writable=True)
        self.global_(f"control_time_g{g}", -1)
        self.global_(f"state_boot_g{g}", 0)
        select = self.shader(f"G{g}Select")
        self.s(
            select,
            [
                "cs = " + path + "/select.hlsl",
                "cs-t96 = null",
                "cs-t24 = ref " + self.r("BindInput"),
                "cs-t95 = ref " + self.r("NativeInput"),
                "cs-t86 = ref " + self.r("Blend"),
                "cs-t87 = ref " + self.r("BodyVerts"),
                "cs-t2 = ref " + self.r("SkinPalette_s1"),
                "cs-t3 = ref " + self.r("SkinPalette_s2"),
                "cs-t82 = ref " + r("Config"),
                "cs-t94 = ref " + r("Map"),
                "cs-u6 = ref " + r("Decision"),
                "Dispatch = 1, 1, 1",
                "cs-u6 = null",
            ],
        )
        for slot in (1, 2):
            self.global_(f"boot_g{g}_s{slot}", 0)
            for name, count, stride, fmt in [
                ("Particles", p, 32, None),
                ("Weights", p, 32, None),
                ("NodeContacts", p * 3, None, "R32G32B32A32_FLOAT"),
                ("PreviousRows", p * 3, None, "R32G32B32A32_FLOAT"),
                ("Lambdas", edges, None, "R32_FLOAT"),
                ("Heads", 4096, None, "R32_UINT"),
                ("Links", p, None, "R32_UINT"),
                ("Exterior", body, None, "R32_UINT"),
                ("BodyContacts", body, None, "R32_UINT"),
                ("HullPoints", body * 3, None, "R32G32B32A32_FLOAT"),
                ("HullCount", 1, None, "R32_UINT"),
            ]:
                self.resource(f"G{g}_{name}_s{slot}", count, fmt, stride, writable=True)
            self.resource(f"G{g}_Snapshot_s{slot}", stride=32)
            params = lambda phase, slot=slot: self.parameters(slot, phase)
            bind = self.bindings(g, slot)
            uavs = [
                f"cs-u{i} = ref " + r(name if name == "State" else f"{name}_s{slot}")
                for i, name in enumerate(
                    [
                        "Particles",
                        "State",
                        "Heads",
                        "Links",
                        "Lambdas",
                        "NodeContacts",
                        "PreviousRows",
                        "Weights",
                    ]
                )
            ]
            call = lambda tag, slot=slot: self.shader(f"G{g}{tag}_s{slot}")
            run = lambda tag, call=call: "run = " + call(tag)
            for tag, phase, file, groups in [
                ("Control", 0, "cloth_nodes", 1),
                ("Frame", 8, "cloth_nodes", 1),
                ("Advect", 1, "cloth_nodes", (p + 63) // 64),
                ("Predict", 2, "cloth_groups", 1),
                ("Edges", 3, "cloth_groups", 1),
                ("Body", 4, "cloth_nodes", (p + 63) // 64),
                ("Velocity", 7, "cloth_nodes", (p + 63) // 64),
                ("Weights", 6, "cloth_nodes", (p + 63) // 64),
            ]:
                self.s(
                    call(tag),
                    ["cs = " + path + "/" + file + ".hlsl"]
                    + params(phase)
                    + bind
                    + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                    + uavs
                    + [f"Dispatch = {groups}, 1, 1"]
                    + self.clear_uavs(),
                )
            for tag, file, target, slot_to_clear in [
                ("Exterior", "exterior", "Exterior", 106),
                ("Contacts", "contacts", "BodyContacts", 112),
            ]:
                self.s(
                    call(tag),
                    ["cs = " + path + "/" + file + ".hlsl"]
                    + params(0)
                    + bind
                    + [
                        f"cs-t{slot_to_clear} = null",
                        "cs-u0 = ref " + r(f"{target}_s{slot}"),
                        f"Dispatch = {(body+127)//128}, 1, 1",
                        "cs-u0 = null",
                    ],
                )
            for tag, mode, groups in [
                ("HullClear", 0, 1),
                ("Hull", 1, (body + 255) // 256),
            ]:
                self.s(
                    call(tag),
                    ["cs = " + path + "/hull.hlsl"]
                    + params(0)
                    + bind
                    + [
                        f"y119 = {mode}",
                        "cs-t118 = null",
                        "cs-t119 = null",
                        "cs-t77 = ref " + r("State"),
                        "cs-u0 = ref " + r(f"HullPoints_s{slot}"),
                        "cs-u1 = ref " + r(f"HullCount_s{slot}"),
                        f"Dispatch = {groups}, 1, 1",
                        "cs-u0 = null",
                        "cs-u1 = null",
                        "cs-t77 = null",
                    ],
                )
            offsets = data["face_offsets"]
            for index in range(len(offsets) - 1):
                count = int(offsets[index + 1] - offsets[index])
                self.s(
                    call("Face" + str(index)),
                    ["cs = " + path + "/cloth_nodes.hlsl"]
                    + params(5)
                    + [f"y118 = {int(offsets[index])}", f"z118 = {count}"]
                    + bind
                    + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                    + uavs
                    + [f"Dispatch = {(count+63)//64}, 1, 1"]
                    + self.clear_uavs(),
                )
            for tag, phase, groups in [
                ("SelfClear", 10, 64),
                ("SelfHash", 11, (p + 63) // 64),
                ("Self", 12, (p + 63) // 64),
            ]:
                self.s(
                    call(tag),
                    ["cs = " + path + "/self.hlsl"]
                    + params(phase)
                    + bind
                    + [
                        "cs-t76 = ref " + r(f"Snapshot_s{slot}"),
                        "cs-t77 = ref " + r("State"),
                        "cs-t78 = ref " + r(f"NodeContacts_s{slot}"),
                        "cs-u0 = ref " + r(f"Particles_s{slot}"),
                        "cs-u2 = ref " + r(f"Heads_s{slot}"),
                        "cs-u3 = ref " + r(f"Links_s{slot}"),
                        f"Dispatch = {groups}, 1, 1",
                        "cs-u0 = null",
                        "cs-u2 = null",
                        "cs-u3 = null",
                        "cs-t76 = null",
                        "cs-t77 = null",
                        "cs-t78 = null",
                    ],
                )
            self.s(
                call("Apply"),
                ["cs = " + path + "/apply.hlsl"]
                + bind
                + [
                    "cs-t76 = ref " + r(f"Particles_s{slot}"),
                    "cs-t77 = ref " + r("State"),
                    "cs-u0 = ref " + self.r(f"Write_s{slot}"),
                    f'Dispatch = {(self.meta["vertices"]+255)//256}, 1, 1',
                    "cs-u0 = null",
                    "cs-t76 = null",
                    "cs-t77 = null",
                ],
            )
            lines = [
                f"if {self.var}state_boot_g{g} == 0",
                "\tclear = " + r("State"),
                f"\t{self.var}state_boot_g{g} = 1",
                "endif",
                f"if {self.var}boot_g{g}_s{slot} == 0",
            ]
            lines += [
                "\tclear = " + r(f"{name}_s{slot}")
                for name in (
                    "Particles",
                    "Weights",
                    "NodeContacts",
                    "PreviousRows",
                    "Lambdas",
                )
            ]
            lines += [
                f"\t{self.var}boot_g{g}_s{slot} = 1",
                "endif",
                "run = " + select,
                run("Exterior"),
                run("Contacts"),
                f"if {self.var}control_time_g{g} != time",
                "\t" + run("Control"),
                f"\t{self.var}control_time_g{g} = time",
                "endif",
                run("Frame"),
                run("Advect"),
            ]
            for micro in range(3):
                lines += [
                    f"{self.var}micro = {micro}",
                    run("HullClear"),
                    run("Hull"),
                    run("Predict"),
                    run("Body"),
                    run("Edges"),
                    run("Body"),
                    run("Velocity"),
                ]
            lines += [
                r(f"Snapshot_s{slot}") + " = copy " + r(f"Particles_s{slot}"),
                run("SelfClear"),
                run("SelfHash"),
                run("Self"),
                run("Body"),
            ]
            for _ in range(4):
                lines += [run("Face" + str(i)) for i in range(len(offsets) - 1)]
            lines += [run("Velocity"), run("Weights"), run("Apply")]
            self.s(self.cmd(f"G{g}Simulate_s{slot}"), lines)

    def emit(self):
        m = self.meta
        v = m["vertices"]
        stride = m["stride"]
        paths = m["paths"]
        self.resource("NativeInput", stride=stride)
        if m["dynamic_bind"] is not None:
            self.resource("BindInput", stride=stride)
        else:
            self.resource(
                "BindInput", count=v, stride=stride, file=paths["bind"], writable=True
            )
            self.resource("RawBindInput", stride=stride)
            self.s(
                self.shader("CopyBind"),
                [
                    "cs = " + paths["copy_bind_shader"],
                    "cs-t24 = ref " + self.r("RawBindInput"),
                    "cs-t111 = ref " + self.r("BindSource"),
                    "cs-u0 = ref " + self.r("BindInput"),
                    f"Dispatch = {(v+255)//256}, 1, 1",
                    "cs-u0 = null",
                    "cs-t111 = null",
                ],
            )
        self.resource("DetectInput", stride=16)
        self.resource("BindSource", stride=stride, file=paths["bind"])
        self.resource("Blend", fmt="R32G32B32A32_FLOAT", file=paths["blend"])
        self.resource("BodyVerts", fmt="R32_UINT", file=paths["body_vertices"])
        self.resource("BodyOrder", fmt="R32_UINT", file=paths["body_order"])
        for name, value in [
            ("slot", 1),
            ("frame_time", -1),
            ("previous_time", -1),
            ("dt", 1 / 60),
            ("micro", 0),
            ("need_render", 0),
        ]:
            self.global_(name, value)
        for slot in (1, 2):
            self.global_(f"ready_s{slot}", -1)
            self.resource(f"SkinSource_s{slot}", stride=stride, alias=True)
            self.resource(
                f"SkinPalette_s{slot}",
                m["palette_rows"][slot],
                "R32G32B32A32_FLOAT",
            )
            self.sections[f'[{self.r(f"SkinPalette_s{slot}")}]'].append(
                "bind_flags = shader_resource"
            )
            self.resource(f"Output_s{slot}", v, stride=stride, vertex=True)
            self.resource(f"Write_s{slot}", fmt="R32_FLOAT", stride=4, alias=True)
            for name in ("Native", "Published"):
                self.resource(f"{name}_s{slot}", stride=stride, alias=True)
            self.resource(f"NativeBlend_s{slot}", alias=True)
            self.resource(
                f"Bounds_s{slot}", m["leaves"] * 4, "R32G32B32A32_FLOAT", writable=True
            )
            for tag, phase, groups in [
                ("Leaves", 0, m["leaves"] // 32),
                ("Roots", 1, max(1, (m["leaves"] // 32 + 30) // 32)),
            ]:
                self.s(
                    self.shader(f"{tag}_s{slot}"),
                    [
                        "cs = " + paths["bounds_shader"],
                        "x115 = 1",
                        f'y115 = {m["leaves"]}',
                        f"z115 = {phase}",
                        "cs-t24 = ref " + self.r("NativeInput"),
                        "cs-t87 = ref " + self.r("BodyVerts"),
                        "cs-t89 = ref " + self.r("BodyOrder"),
                        "cs-t91 = null",
                        "cs-u4 = ref " + self.r(f"Bounds_s{slot}"),
                        f"Dispatch = {groups}, 1, 1",
                        "cs-u4 = null",
                    ],
                )
        for g, data in enumerate(m["garments"]):
            self.garment(g, data)
        source = lambda slot: self.r(f"SkinSource_s{slot}")
        effective = f"{self.enabled} == 1 && {self.mode} >= 2"
        lines = [
            "if " + effective,
            f"\tif {self.var}frame_time != time",
            f"\t\t{self.var}frame_time = time",
            f"\t\t{self.var}dt = time - {self.var}previous_time",
            f"\t\tif {self.var}previous_time < 0 || {self.var}dt > 0.05 || {self.var}dt <= 0",
            f"\t\t\t{self.var}dt = 0.0166667",
            "\t\tendif",
            f"\t\t{self.var}previous_time = time",
            f"\t\t{self.var}slot = 1",
            f'\t\tif $zz_ms_occ_{m["occ"]} == 2',
            f"\t\t\t{self.var}slot = 2",
            "\t\tendif",
            "\tendif",
            f"\tif vb0 === {source(1)}",
            f"\t\t{self.var}slot = 1",
            f"\telif vb0 === {source(2)}",
            f"\t\t{self.var}slot = 2",
            "\tendif",
        ]
        for slot in (1, 2):
            lines += [
                f"\tif {self.var}slot == {slot}",
                f"\t\t{self.var}need_render = 0",
                f"\t\tif {self.var}ready_s{slot} != time",
                f"\t\t\t{self.var}need_render = 1",
                "\t\telif vb0 === " + self.r(f"Native_s{slot}"),
                "\t\telif vb0 === " + self.r(f"Published_s{slot}"),
                "\t\telse",
                f"\t\t\t{self.var}need_render = 1",
                "\t\tendif",
                f"\t\tif {self.var}need_render == 1",
                "\t\t\t"
                + self.r(f"Native_s{slot}")
                + " = ref "
                + source(slot)
                + " unless_null",
                "\t\t\t" + self.r(f"NativeBlend_s{slot}") + " = ref vb2",
                "\t\t\t"
                + self.r(f"Output_s{slot}")
                + " = copy "
                + source(slot)
                + " unless_null",
                "\t\t\t"
                + self.r("NativeInput")
                + " = copy "
                + self.r(f"Output_s{slot}"),
                "\t\t\t"
                + (
                    self.r("BindInput") + " = copy " + m["dynamic_bind"]
                    if m["dynamic_bind"] is not None
                    else self.r("RawBindInput")
                    + " = copy "
                    + self.comp["base_resource"]
                ),
                "\t\t\t"
                + self.r("DetectInput")
                + f" = copy ResourceDragPinnedComponentInfo_{self.suffix}",
                "\t\t\t"
                + self.r(f"Write_s{slot}")
                + " = ref "
                + self.r(f"Output_s{slot}"),
                "\t\t\trun = " + self.shader(f"Leaves_s{slot}"),
                "\t\t\trun = " + self.shader(f"Roots_s{slot}"),
            ]
            if m["dynamic_bind"] is None:
                insertion = next(
                    i
                    for i, line in enumerate(lines)
                    if line.strip() == "run = " + self.shader(f"Leaves_s{slot}")
                )
                lines.insert(insertion, "\t\t\trun = " + self.shader("CopyBind"))
            lines += [
                "\t\t\trun = " + self.cmd(f"G{g}Simulate_s{slot}")
                for g in range(len(m["garments"]))
            ]
            lines += [
                f"\t\t\t{self.var}ready_s{slot} = time",
                "\t\tendif",
                "\t\tvb0 = ref " + self.r(f"Output_s{slot}"),
                "\t\t" + self.r(f"Published_s{slot}") + " = ref vb0",
                "\tendif",
            ]
        self.s(self.cmd("Bind"), lines + ["endif"])
        for action in ("Publish", "Restore"):
            lines = ["if " + effective]
            for slot in (1, 2):
                target = (
                    self.r(f"Output_s{slot}") if action == "Publish" else source(slot)
                )
                lines += [
                    f"\tif {self.var}slot == {slot}",
                    "\t\tvb0 = ref " + target + " unless_null",
                    "\t\tvb2 = ref " + self.r(f"NativeBlend_s{slot}") + " unless_null",
                    "\tendif",
                ]
            self.s(self.cmd(action), lines + ["endif"])
        return self


def emit_components(node, sections, components, ns):
    active = [comp for comp in components if comp.get("cloth")]
    if not active:
        return
    constants = sections.setdefault("[Constants]", [])
    prefix = "$ssmtcloth_" + ns + "_"
    settings = {
        "enabled": int(bool(getattr(node, "cloth_runtime_enabled", False))),
        "reset": 0,
        "drag_sensitivity": 2.0,
        "stretch_compliance": 0.00003,
        "bend_compliance": 0.0008,
        "velocity_retention": 0.85,
        "bend_min_ratio": 0.55,
        "fold_height": 0.012,
        "fold_spacing": 0.075,
        "gather_strength": 0.8,
    }
    for key, default in settings.items():
        value = (
            getattr(node, "cloth_" + key, default)
            if key not in ("enabled", "reset")
            else default
        )
        line = "global " + prefix + key + " = " + str(value)
        if line not in constants:
            constants.append(line)
    mode = node._runtime_variable_names(ns)[0]
    sections[f"[KeySSMTClothMode_{ns}]"] = [
        f"condition = $ssmtdrag_drawn_{ns} == 1",
        "key = " + str(getattr(node, "cloth_toggle_key", "ctrl alt U")),
        "type = cycle",
        prefix + "enabled = 0, 1",
        mode + " = 2, 2",
    ]
    sections[f"[KeySSMTClothReset_{ns}]"] = [
        "condition = " + prefix + "enabled == 1",
        "key = " + str(getattr(node, "cloth_reset_key", "ctrl alt VK_BACK")),
        "type = cycle",
        prefix + "reset = 0, 1",
    ]
    inserts = defaultdict(lambda: defaultdict(list))
    for comp in active:
        emitter = _Emitter(node, sections, comp, ns).emit()
        for section, index, slot, palette, vertices in comp["cloth"]["skin_writes"]:
            line = sections[section][index]
            indent = line[: len(line) - len(line.lstrip())]
            inserts[section][index] += [
                indent + DRAW_BEGIN,
                indent + emitter.r(f"SkinSource_s{slot}") + f" = ref {vertices}",
                indent + emitter.r(f"SkinPalette_s{slot}") + f" = copy {palette}",
                indent + emitter.var + f"ready_s{slot} = -1",
                indent + DRAW_END,
            ]
        comp["cloth_bind"] = emitter.cmd("Bind")
        comp["cloth_publish"] = emitter.cmd("Publish")
        comp["cloth_restore"] = emitter.cmd("Restore")
    for section, locations in inserts.items():
        for index in sorted(locations, reverse=True):
            sections[section][index:index] = locations[index]


def publish_before_draws(sections, comp):
    """Republish after every possible legacy TTL rebind, then restore native VB."""
    publish = comp.get("cloth_publish")
    if not publish:
        return
    roots = component_sections(comp)
    pending = list(roots)
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        lines = clean_tail("\n".join(sections.get(name, []))).splitlines()
        if name in roots and not any(
            line.strip() == "run = " + comp["cloth_bind"] for line in lines
        ):
            lines[0:0] = [
                DRAW_BEGIN,
                "run = " + comp["cloth_bind"],
                "post run = " + comp["cloth_restore"],
                DRAW_END,
            ]
        out = []
        for line in lines:
            match = re.match(
                r"\s*run\s*=\s*(CommandListSSMTTTLDraw_\S+)", line, re.IGNORECASE
            )
            if match:
                pending.append("[" + match.group(1) + "]")
            if re.match(
                r"\s*(?:drawindexed(?:instanced)?\s*=|run\s*=\s*CommandList(?:SSMTTTLDraw_|\\TTL\\Draw\b))",
                line,
                re.IGNORECASE,
            ):
                indent = line[: len(line) - len(line.lstrip())]
                out += [
                    indent + DRAW_BEGIN,
                    indent + "run = " + publish,
                    indent + DRAW_END,
                ]
            out.append(line)
        sections[name] = out
