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
from .drag_cloth_path import exported_path
from .drag_cloth_regions import reference_branches, cap_domain
from .drag_collision import bake_sweep_order

TOOLSET = Path(__file__).resolve().parents[1] / "Toolset" / "drag_cloth"
SHADERS = (
    "cloth_nodes.hlsl",
    "cloth_groups.hlsl",
    "parallel_faces.hlsl",
    "route.hlsl",
    "precision.hlsl",
    "level_candidates.hlsl",
    "retain.hlsl",
    "startup.hlsl",
    "pose_refresh.hlsl",
    "shorts_bind.hlsl",
    "frozen_skin.hlsl",
    "frozen_display.hlsl",
    "display_normals.hlsl",
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
    if name in ("shorts_bind.hlsl", "frozen_skin.hlsl", "frozen_display.hlsl"):
        shared = _shader(node, "cloth_nodes.hlsl", constants).replace(
            "RWBuffer<uint> Heads : register(u2);", ""
        )
        if name in ("frozen_display.hlsl", "frozen_skin.hlsl"):
            shared = shared.replace("RWBuffer<uint> Links : register(u3);", "")
        return (TOOLSET / name).read_text("utf8").replace('#include "cloth_nodes.hlsl"', shared)
    if name == "startup.hlsl":
        return (TOOLSET / name).read_text("utf8").replace(
            '#include "cloth_nodes.hlsl"', _shader(node, "cloth_nodes.hlsl", constants)
        )
    if name == "parallel_faces.hlsl":
        # Reuse the exact per-face contact math. Only its commit writes private
        # face-corner impulses, which a second dispatch gathers without races.
        shared = _shader(node, "cloth_nodes.hlsl", constants)
        first = shared.index("void FaceScoped(")
        last = shared.index("bool FaceBodyIntersection", first)
        block = shared[first:last]
        writer = (
            "Particle particle=Particles[ids[k]];particle.current.xyz=p[k];"
            "particle.previous.xyz+=delta;Particles[ids[k]]=particle;"
        )
        if block.count(writer) != 1:
            raise ValueError("Unexpected cloth face commit; cannot specialize parallel contacts")
        commit = "FaceImpulses[face*3+k]=float4(delta,mass[k]>.5 && dot(delta,delta)>1e-18?1:0);"
        shared = (shared[:first] + block.replace(writer, commit) + shared[last:]).replace(
            "RWBuffer<uint> Heads : register(u2);", ""
        )
        return (TOOLSET / name).read_text("utf8").replace(
            '#include "cloth_nodes.hlsl"', shared
        )
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


def _control_regions(
    node, root, sections, comp, names, triangles, cloth, base, vertex_count, name_key
):
    entries = node._collect_enabled_zone_entries()
    ids_path = (
        Path(root)
        / node._buffer_dir(sections, comp)
        / (comp["base_name"] + "JiggleZoneIDs.buf")
    )
    weights_path = ids_path.with_name(comp["base_name"] + "JiggleZoneWeights.buf")
    ids = weights = None
    if ids_path.is_file() and weights_path.is_file():
        ids = np.fromfile(ids_path, "<u4").reshape(-1, 4)
        weights = np.fromfile(weights_path, "<f4").reshape(-1, 4)
    regions = []
    for zone, empty in entries:
        settings = empty.ssmt_drag_zone
        selected_names = {
            name_key(item.object.name)
            for item in getattr(settings, "include_objects", ())
            if getattr(item, "object", None) is not None
        }
        selected = cloth & np.isin(names, list(selected_names))
        if not selected.any():
            continue
        collider_names = {
            name_key(item.object.name)
            for item in getattr(settings, "collider_objects", ())
            if getattr(item, "object", None) is not None
        }
        body = np.isin(names, list(collider_names))
        if not body.any():
            raise ValueError(f"上下拖拽：区域 {zone} 的碰撞体不在衣物所在合并载体内")
        rows = np.zeros(vertex_count, bool)
        rows[np.unique(triangles[selected])] = True
        if ids is not None and len(ids) >= base + vertex_count:
            rows &= np.any(
                (ids[base : base + vertex_count] == int(zone))
                & (weights[base : base + vertex_count] > 1e-6),
                axis=1,
            )
        direction = str(getattr(settings, "cloth_direction", "AUTO"))
        code = {"AUTO": 3, "NEGATIVE": 1, "POSITIVE": 2}.get(direction)
        if code is None:
            raise ValueError("无法识别区域的穿脱方向：" + direction)
        path = None
        if (
            getattr(settings, "cloth_path_start", None) is not None
            or getattr(settings, "cloth_path_end", None) is not None
        ):
            path = exported_path(
                settings,
                node._get_reference_matrix_inv(comp),
                node._get_export_space_matrix(),
                node._get_non_mirror_mirror(),
            )
        regions.append((int(zone), code, rows, selected, body, path))
    return regions


def _region_batches(regions):
    """Two handles on the same clothes share state; disjoint clothes never weld."""
    batches = []
    for region in regions:
        selected, body = region[3:5]
        overlaps = [i for i, batch in enumerate(batches) if np.any(batch[0] & selected)]
        controls = [region]
        selected = selected.copy()
        for i in reversed(overlaps):
            previous, collider, linked = batches.pop(i)
            if not np.array_equal(body, collider):
                raise ValueError("上下拖拽：同一衣物的多个区域必须选择相同碰撞体")
            selected |= previous
            controls.extend(linked)
        batches.append((selected, body, controls))
    return batches


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
            section_name.startswith(f"[TextureOverride_{comp['hash']}_")
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
    regions = _control_regions(
        node,
        root,
        sections,
        comp,
        names,
        triangles,
        cloth,
        base,
        len(positions),
        name_key,
    )
    garments, bodies, garment_regions = [], [], []
    reference_cache = {}
    collider_objects = [
        ref.object
        for _, entry in node._collect_enabled_zone_entries()
        for ref in getattr(entry.ssmt_drag_zone, "collider_objects", ())
        if getattr(ref, "object", None) is not None
    ]
    written_rows = np.zeros(count, bool)
    for selected, collider, controls in _region_batches(regions):
        _, unique = np.unique(
            np.sort(triangles[collider], axis=1), axis=0, return_index=True
        )
        body_faces = triangles[collider][np.sort(unique)]
        reference_body_faces = body_faces
        body_domain, cap_start, branch_reference = None, 0, None
        paths = [path for _, _, _, _, _, path in controls if path is not None]
        if paths:
            reference_key = body_faces.tobytes()
            if reference_key not in reference_cache:
                body_keys = set(names[collider])
                reference_objects = [
                    obj
                    for obj in collider_objects
                    if name_key(getattr(obj, "name_full", obj.name)) in body_keys
                ]
                reference_cache[reference_key] = reference_branches(
                    node,
                    comp,
                    positions,
                    np.unique(body_faces),
                    reference_objects,
                    np.unique(triangles[cloth]),
                )
            branch_reference = reference_cache[reference_key]
            if branch_reference is not None:
                rows = np.unique(body_faces)
                starts = [
                    int(
                        np.argmax(
                            branch_reference[
                                rows[
                                    np.argmin(
                                        np.sum((positions[rows] - path[0]) ** 2, axis=1)
                                    )
                                ]
                            ]
                        )
                    )
                    for path in paths
                ]
                if len(set(starts)) != 1:
                    raise ValueError(
                        "同一穿脱衣物的起点跨越不同人体分支，请使用独立衣物区域"
                    )
                body_domain = starts[0]
                if body_domain in (1, 2):
                    cloth_domains = np.argmax(
                        branch_reference[triangles].mean(axis=1), axis=1
                    )
                    selected = selected & (cloth_domains == body_domain)
                    if not np.any(selected):
                        raise ValueError("袖口起点的人体分区没有匹配的衣物面")
                labels = np.argmax(branch_reference[body_faces].mean(axis=1), axis=1)
                body_faces, cap_start = cap_domain(
                    positions, body_faces[labels == body_domain]
                )
        pool = next(
            (
                i
                for i, item in enumerate(bodies)
                if np.array_equal(item["faces"], body_faces)
            ),
            None,
        )
        if pool is None:
            pool = len(bodies)
            order, leaves = bake_sweep_order(positions[body_faces])
            bodies.append(
                {
                    "faces": body_faces,
                    "order": order,
                    "leaves": leaves,
                    "body_count": len(body_faces),
                    "surface": None,
                    "domain": body_domain,
                    "cap_start": cap_start,
                }
            )
        two_ended = np.zeros(len(positions), np.uint8)
        for _, code, rows, _, _, path in controls:
            if code != 3 and path is None:
                two_ended = np.maximum(
                    two_ended, rows.astype(np.uint8) * (2 if code == 2 else 1)
                )
        if body_domain is not None and bodies[pool]["surface"] is None:
            bodies[pool]["surface"] = np.r_[
                np.ones(cap_start, np.uint32),
                np.zeros(len(body_faces) - cap_start, np.uint32),
            ]
        baked = bake_garments(
            positions,
            normals,
            triangles[selected],
            body_faces,
            count,
            base,
            budget=getattr(node, "cloth_proxy_budget", 2450),
            up_axis=getattr(node, "cloth_up_axis", "AUTO"),
            two_ended_rows=two_ended,
            body_surface=bodies[pool]["surface"],
            blend_weights=weights,
            blend_indices=indices,
            body_domain=body_domain,
            body_branch_weights=branch_reference,
            body_skin_faces=cap_start,
            reference_body_faces=reference_body_faces,
            manual_paths=[
                (zone, rows, path)
                for zone, _, rows, _, _, path in controls
                if path is not None
            ],
        )
        bodies[pool]["surface"] = baked[0]["surface_mask"]
        for data in baked:
            owned = data["map"] != 0xFFFFFFFF
            if np.any(written_rows & owned):
                raise ValueError(
                    "上下拖拽：独立衣物共享了导出顶点，请拆开衣物的网格顶点"
                )
            written_rows |= owned
            data["body_index"] = pool
            garments.append(data)
            garment_regions.append(controls)
    if not garments:
        raise ValueError("上下拖拽：区域没有匹配到有效衣物")
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
    selected_rows = np.unique(
        np.concatenate(
            [item["faces"].ravel() for item in bodies] + [triangles[cloth].ravel()]
        )
    )
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
        ]
    }
    for i, pool in enumerate(bodies):
        prefix = f"b{i}/" if i else ""
        pool["paths"] = {
            "body_vertices": assets.put(
                prefix + "body_vertices.buf", (pool["faces"] + base).astype(np.uint32)
            ),
            "body_order": assets.put(prefix + "body_order.buf", pool["order"]),
        }
    paths.update(bodies[0]["paths"])
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
    paths["copy_native_shader"] = assets.put(
        "copy_native.hlsl",
        f"#define COPY_WORDS {count * stride // 4}\n"
        + (TOOLSET / "copy_native.hlsl").read_text("utf8"),
    )
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
        cfg[2, 2] = (
            min(max(float(getattr(node, "cloth_surface_gap_mm", 1.0)), 0), 30) * 0.001
        )
        body_faces = bodies[data["body_index"]]["faces"]
        old_span = float(cfg[13, 3])
        if cfg[13, 2] > 0.5:
            cfg[13, 3] = float(getattr(node, "cloth_min_span_ratio", 0.05))
            for zone, (code, maximum) in data.get("path_controls", {}).items():
                maximum = maximum * (1 - old_span) / (1 - cfg[13, 3])
                if maximum > 1.00001:
                    raise ValueError(
                        "穿脱箭头超过双端可用长度，请缩短行程或减小双端最小保留长度"
                    )
                data["path_controls"][zone] = (code, min(1.0, maximum))
        controls = np.zeros(
            (max((region[0] for region in regions), default=0) + 1, 4), np.float32
        )
        garment_rows = data["map"][base : base + len(positions)] != 0xFFFFFFFF
        for zone, code, rows, _, _, _ in garment_regions[index]:
            if np.any(garment_rows[: len(rows)] & rows):
                limit = data.get("path_controls", {}).get(zone)
                controls[zone] = (
                    [code, 1, 0, 0] if limit is None else [limit[0], limit[1], 0, 1]
                )
        data["control_zones"] = controls
        pool = bodies[data["body_index"]]
        leaves = int(pool["leaves"])
        owner_bounds = np.zeros(leaves * 2, np.uint32)
        ordered = pool["order"]
        owner_bounds[leaves : leaves + len(ordered)] = np.where(
            data["surface_mask"][ordered] != 0,
            np.left_shift(np.uint32(1), data["body_regions"][ordered]),
            0,
        )
        level = leaves // 2
        while level:
            owner_bounds[level : level * 2] = (
                owner_bounds[level * 2 : level * 4 : 2]
                | owner_bounds[level * 2 + 1 : level * 4 : 2]
            )
            level //= 2
        data["body_owner_bounds"] = owner_bounds
        if cfg[13, 2] > 0.5 and 0.5 < cfg[14, 3] < 1.5:
            cfg[14, 0] = max(0.0, cfg[14, 2] - cfg[13, 3])
        p = int(cfg[0, 0])
        h = int(cfg[1, 3])
        ec = int(cfg[1, 1])
        fc = int(cfg[1, 2])
        if cfg[14, 3] < 1.5 and not data.get("path_controls"):
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
        lo = float(ranges.min() / max(cfg[6, 3], 1e-6))
        hi = float(ranges.max() / max(cfg[6, 3], 1e-6))
        data["level_config"] = np.array([[lo, hi, 128 / max(hi-lo, 1e-5), 128 if cfg[15, 2] > .5 and not (cfg.shape[0] > 16 and cfg[16, 3] > 1.5) else 0]], np.float32)
        intervals = sorted(zip(ranges.min(1), ranges.max(1)))
        minimum = 0.0 if cfg[13, 2] > 0.5 else float(cfg[6, 3] * cfg[4, 0])
        if cfg[14, 3] > 0.5 and cfg[14, 3] < 1.5:
            minimum, maximum = 0.0, float(cfg[6, 3] * cfg[14, 0])
        else:
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
                ("CLOTH_REFERENCE_FLOW", int(cfg.shape[0] > 16 and cfg[16, 3] > 1.5)),
                ("CLOTH_ANATOMICAL_SCOPE", int(cfg.shape[0] > 16 and cfg[16, 0] > 0.5)),
                ("CLOTH_CLOSED_DOMAIN", int(cfg.shape[0] > 16 and cfg[16, 3] > 0.5)),
            ]
        )
        hem_map = data["hem_map"]
        upper = int(cfg[13, 0]) if cfg[13, 2] > 0.5 else h
        foot = ((hem_map >= upper) & (hem_map < h)) if cfg[13, 2] > 0.5 else data["rest"][1::2, 2] < .065
        top = hem_map < upper
        data["frame_axes"] = np.concatenate((
            np.array([int(foot.sum()), int(top.sum())], dtype=np.uint32),
            data["representatives"][foot], data["representatives"][top],
        )).astype(np.uint32)
        for name, value in data.items():
            if isinstance(value, np.ndarray):
                assets.put(f"g{index}/{name}.buf", value)
        for name in SHADERS:
            if name == "shorts_bind.hlsl" and cfg[14, 3] != 2:
                continue
            if name == "self.hlsl" and not getattr(node, "cloth_self_collision", False):
                continue
            if name in ("parallel_faces.hlsl", "route.hlsl", "precision.hlsl", "retain.hlsl") and not (cfg.shape[0] > 16 and cfg[16, 3] > 0.5):
                continue
            assets.put(f"g{index}/{name}", _shader(node, name, constants))
            if name == "cloth_nodes.hlsl":
                for phase in (0, 1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
                    assets.put(
                        f"g{index}/cloth_nodes_p{phase}.hlsl",
                        f"#define STATIC_PHASE {phase}\n"
                        + _shader(node, name, constants),
                    )
    assets.finish()
    return {
        "token": token,
        "directory": relative,
        "paths": paths,
        "garments": garments,
        "leaves": bodies[0]["leaves"],
        "body_count": bodies[0]["body_count"],
        "bodies": bodies,
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
        self.bodies = self.meta.get("bodies") or [self.meta]
        self.suffix = comp["comp_name"] + "_" + ns
        self.prefix = "ResourceSSMTCloth_" + self.suffix + "_"
        self.var = "$ssmtcloth_" + ns + "_" + comp["comp_name"] + "_"
        self.enabled = "$ssmtcloth_" + ns + "_enabled"
        self.reset = "$ssmtcloth_" + ns + "_reset"
        self.mode = node._runtime_variable_names(ns)[0]
        self.self_collision = bool(getattr(node, "cloth_self_collision", False))

    def r(self, name):
        return self.prefix + name

    def body_index(self, g):
        return self.meta["garments"][g].get("body_index", 0)

    def body_name(self, index, name):
        return self.r((f"B{index}_" if index else "") + name)

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
            lines += [
                "bind_flags = unordered_access vertex_buffer",
                "misc_flags = none",
            ]
        elif writable:
            lines += ["bind_flags = shader_resource unordered_access"]
        self.s(self.r(name), lines)

    def parameters(self, slot, phase, g=0):
        v = "$ssmtcloth_" + self.ns + "_"
        return [
            "x115 = 1",
            f"y115 = {self.bodies[self.body_index(g)]['leaves']}",
            f"x116 = {phase}",
            f"y116 = {self.enabled}",
            f"z116 = {self.reset}",
            "w116 = $cursorY",
            "x118 = $cursorX",
            "x117 = $isMouseButtonDown",
            f"y117 = $screenH / {v}drag_sensitivity",
            f"w117 = $screenW / {v}drag_sensitivity",
            f"z117 = {self.var}dt",
            f"w118 = {slot}",
            f"x119 = {self.var}micro",
            f"z119 = {self.var}micro_stage",
            f"w119 = {self.var}repair_g{g}",
            f"x121 = {v}stretch_compliance",
            f"y121 = {v}bend_compliance",
            f"z121 = {v}velocity_retention",
            f"w121 = {v}bend_min_ratio",
            f"x122 = {v}fold_height",
            f"y122 = {v}fold_spacing",
            f"z122 = {v}gather_strength",
            "w122 = 1",
            f"x123 = {v}surface_gap_mm * 0.001",
            f"y123 = {v}sleep_enabled",
            f"z123 = {v}sleep_pose_mm * 0.001",
            f"w123 = {v}sleep_recheck_s",
            "x124 = time",
            "y124 = 1",
            f"z124 = {g}",
            f"x125 = {v}adaptive_quality",
            f"y125 = {self.var}transport_g{g}",
            f"z125 = {self.var}edge_repeat_g{g}",
            f"x126 = {v}level_search * (1 - {self.var}passive)",
            f"z126 = {self.var}passive",
            f"w126 = {self.var}control_due_g{g}",
            "y127 = 0",
        ]

    def bindings(self, g, slot):
        r = lambda name: self.r(f"G{g}_" + name)
        body = lambda name: self.body_name(self.body_index(g), name)
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
            84: r("SkinMix"),
            85: r("Representatives"),
            86: self.r("Blend"),
            87: body("BodyVerts"),
            89: body("BodyOrder"),
            91: body(f"Bounds_s{slot}"),
            94: r("Map"),
            95: r("FrameAxes"),
            96: r("Decision"),
            98: r("LevelConfig"),
            100: r("FaceColors"),
            101: r("ReferenceBinding"),
            102: r("ReferenceRows"),
            103: r(f"LevelCandidates_s{slot}"),
            104: r("BodyLevels"),
            105: r("BodyAdjacency"),
            106: body(f"Exterior_s{slot}"),
            107: r("HemLines"),
            108: r("SurfaceMask"),
            109: r("BodyOwners"),
            110: r("ControlZones"),
            111: self.r("BindInput"),
            112: r(f"BodyContacts_s{slot}"),
            113: r("Edges"),
            114: r("EdgeOffsets"),
            115: r("FaceOffsets"),
            116: r("HemMap"),
            117: r("HemParams"),
            118: r(f"HullPoints_s{slot}"),
            119: r(f"HullCount_s{slot}"),
            121: r("BodySeeds"),
            122: r("BodyDirections"),
            123: r("HemArc"),
            124: r("BodyChannels"),
            125: r("BodyOwnerBounds"),
            126: r("AttachmentRoles"),
            127: r("CollisionMask"),
        }
        return [f"cs-t{i} = ref {name}" for i, name in values.items()]

    @staticmethod
    def clear_uavs():
        return [f"cs-u{i} = null" for i in range(8)]

    def garment(self, g, data):
        cfg = data["config"]
        p = int(cfg[0, 0])
        edges = int(cfg[1, 0])
        body = self.bodies[self.body_index(g)]["body_count"]
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
            "FrameAxes": ("frame_axes", "R32_UINT"),
            "LevelConfig": ("level_config", "R32G32B32A32_FLOAT"),
            "HemParams": ("hem_params", "R32G32B32A32_FLOAT"),
            "HemLines": ("hem_lines", "R32G32B32A32_FLOAT"),
            "HemArc": ("hem_arc", "R32_FLOAT"),
            "BodyAdjacency": ("body_adjacency", "R32_UINT"),
            "SurfaceMask": ("surface_mask", "R32_UINT"),
            "CollisionMask": ("collision_mask", "R32_UINT"),
            "BodyOwners": ("body_regions", "R32_UINT"),
            "DisplayTriangles": ("display_triangles", "R32_UINT"),
            "DisplayWeights": ("display_weights", "R32G32B32A32_FLOAT"),
            "ControlZones": ("control_zones", "R32G32B32A32_FLOAT"),
            "BodySeeds": ("body_seeds", "R32_UINT"),
            "BodyDirections": ("body_directions", "R32G32B32A32_FLOAT"),
            "BodyChannels": ("body_channels", "R32G32B32A32_FLOAT"),
            "BodyOwnerBounds": ("body_owner_bounds", "R32_UINT"),
            "BodyLevels": ("body_levels", "R32_FLOAT"),
            "ReferenceBinding": ("reference_binding", "R32G32B32A32_FLOAT"),
            "ReferenceRows": ("reference_rows", "R32_UINT"),
            "AttachmentRoles": ("attachment_roles", "R32_UINT"),
            "SkinMix": ("skin_mix", "R32_FLOAT"),
        }
        for name, (file, fmt) in static.items():
            self.resource(f"G{g}_" + name, fmt=fmt, file=path + "/" + file + ".buf")
        quality_enabled = cfg.shape[0] > 16 and cfg[16, 3] > 0.5
        quality_rows = 2 * (p + int(cfg[0, 2])) if quality_enabled else 0
        self.resource(
            f"G{g}_State", 220 + quality_rows, "R32G32B32A32_FLOAT", writable=True
        )
        self.resource(f"G{g}_Decision", 3, "R32G32B32A32_FLOAT", writable=True)
        self.global_(f"control_time_g{g}", -1)
        self.global_(f"state_boot_g{g}", 0)
        self.global_(f"repair_g{g}", 0)
        self.global_(f"transport_g{g}", 0)
        self.global_(f"edge_repeat_g{g}", 0)
        self.global_(f"control_due_g{g}", 0)
        select = self.shader(f"G{g}Select")
        self.s(
            select,
            [
                "cs = " + path + "/select.hlsl",
                "cs-t96 = null",
                "cs-t24 = ref " + self.r("BindInput"),
                "cs-t95 = ref " + self.r("NativeInput"),
                "cs-t86 = ref " + self.r("Blend"),
                "cs-t87 = ref " + self.body_name(self.body_index(g), "BodyVerts"),
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
            self.resource(f"G{g}_FrozenMesh_s{slot}", p, "R32G32B32A32_FLOAT", writable=True)
            self.resource(f"G{g}_FrozenDisplay_s{slot}", self.meta["vertices"], "R32G32B32A32_FLOAT", writable=True)
            self.resource(f"G{g}_DisplayNormals_s{slot}", p, "R32G32B32A32_FLOAT", writable=True)
            if cfg[14, 3] == 2:
                self.resource(f"G{g}_ShortsBinding_s{slot}", p*17, "R32G32B32A32_FLOAT", writable=True)
            bands = int(data["level_config"][0, 3])
            candidate_words = max(1, bands * ((body + 31) // 32))
            self.resource(f"G{g}_LevelCandidates_s{slot}", candidate_words, "R32_UINT", writable=True)
            if quality_enabled:
                self.resource(
                    f"G{g}_FaceImpulses_s{slot}", int(cfg[0, 2]) * 3,
                    "R32G32B32A32_FLOAT", writable=True,
                )
            for name, count, stride, fmt in [
                ("Particles", p, 32, None),
                ("Weights", p, 32, None),
                ("NodeContacts", p * 3, None, "R32G32B32A32_FLOAT"),
                ("PreviousRows", p * 3, None, "R32G32B32A32_FLOAT"),
                ("Lambdas", edges, None, "R32_FLOAT"),
                ("Heads", 4096, None, "R32_UINT"),
                ("Links", p, None, "R32_UINT"),
                ("BodyContacts", body, None, "R32_UINT"),
                (
                    "HullPoints",
                    body * 3 * (2 if cfg[13, 2] > 0.5 else 1),
                    None,
                    "R32G32B32A32_FLOAT",
                ),
                ("HullCount", 2 if cfg[13, 2] > 0.5 else 1, None, "R32_UINT"),
            ]:
                if name in ("Heads", "Links") and not self.self_collision:
                    continue
                self.resource(f"G{g}_{name}_s{slot}", count, fmt, stride, writable=True)
            if self.self_collision:
                self.resource(f"G{g}_Snapshot_s{slot}", stride=32)
            params = lambda phase, slot=slot: self.parameters(slot, phase, g)
            bind = self.bindings(g, slot)
            uavs = [
                (
                    f"cs-u{i} = null"
                    if name in ("Heads", "Links") and not self.self_collision
                    else f"cs-u{i} = ref "
                    + r(name if name == "State" else f"{name}_s{slot}")
                )
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
                ("Recontact", 9, "cloth_nodes", (p + 63) // 64),
                ("Collision", 10, "cloth_nodes", (p + 63) // 64),
                ("Validate", 11, "cloth_nodes", (p + int(cfg[0, 2]) + 63) // 64),
                ("Quality", 12, "cloth_nodes", 1),
                ("Wake", 13, "cloth_nodes", 1),
            ]:
                self.s(
                    call(tag),
                    [
                        "cs = "
                        + path
                        + "/"
                        + (f"cloth_nodes_p{phase}" if file == "cloth_nodes" else file)
                        + ".hlsl"
                    ]
                    + params(phase)
                    + bind
                    + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                    + uavs
                    + [f"Dispatch = {groups}, 1, 1"]
                    + self.clear_uavs(),
                )
            for tag, file, target, slot_to_clear in [
                ("Contacts", "contacts", "BodyContacts", 112),
            ]:
                self.s(
                    call(tag),
                    ["cs = " + path + "/" + file + ".hlsl"]
                    + params(0)
                    + bind
                    + [
                        "cs-t77 = ref " + r("State"),
                        f"cs-t{slot_to_clear} = null",
                        "cs-u0 = ref " + r(f"{target}_s{slot}"),
                        f"Dispatch = {(body + 127) // 128}, 1, 1",
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
            if cfg[14, 3] == 2:
                for tag, phase, file in (("Weights",6,"shorts_bind.hlsl"),):
                    self.s(
                        call(tag), ["cs = " + path + "/" + file] + params(phase) + bind
                        + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                        + uavs + ["cs-u2 = ref " + r(f"ShortsBinding_s{slot}")]
                        + [f"Dispatch = {(p+63)//64}, 1, 1"] + self.clear_uavs(),
                    )
            for tag, mode in (("Skin",0),("Advect",2),("Capture",1)):
                self.s(
                    call(tag), ["cs = " + path + "/frozen_skin.hlsl"] + params(1)
                    + [f"x127 = {mode}"] + bind
                    + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                    + uavs + ["cs-u2 = ref " + r(f"FrozenMesh_s{slot}")]
                    + ["cs-u3 = " + ("ref " + r(f"ShortsBinding_s{slot}") if cfg[14, 3] == 2 else "null")]
                    + [f"Dispatch = {(p+63)//64}, 1, 1"] + self.clear_uavs(),
                )
            self.s(call("CaptureDisplay"), ["cs = " + path + "/frozen_display.hlsl"]
                   + params(1) + bind + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                   + uavs + ["cs-u2 = ref " + r(f"FrozenDisplay_s{slot}"),
                             "cs-u3 = ref " + self.r(f"Write_s{slot}"),
                             f"Dispatch = {(self.meta['vertices']+255)//256}, 1, 1"] + self.clear_uavs())
            self.s(
                call("PoseRefresh"), ["cs = " + path + "/pose_refresh.hlsl"]
                + params(21) + bind + uavs + ["Dispatch = 1, 1, 1"]
                + self.clear_uavs(),
            )
            self.s(
                call("Startup"), ["cs = " + path + "/startup.hlsl"]
                + params(20) + bind + uavs + ["Dispatch = 1, 1, 1"]
                + self.clear_uavs(),
            )
            if bands:
                for tag, stage, count in (("LevelsClear",0,candidate_words),("LevelsFill",1,body)):
                    self.s(
                        call(tag), ["cs = " + path + "/level_candidates.hlsl"]
                        + params(18) + [f"y126 = {stage}"] + bind
                        + ["cs-t103 = null", "cs-t77 = ref " + r("State"), "cs-u2 = ref " + r(f"LevelCandidates_s{slot}")]
                        + [f"Dispatch = {(count+63)//64}, 1, 1", "cs-u2 = null", "cs-t77 = null"],
                    )
            if quality_enabled:
                self.s(
                    call("Precision"), ["cs = " + path + "/precision.hlsl"]
                    + params(17) + bind + uavs + ["Dispatch = 1, 1, 1"]
                    + self.clear_uavs(),
                )
                self.s(
                    call("Route"), ["cs = " + path + "/route.hlsl"]
                    + params(16) + bind + uavs + ["Dispatch = 1, 1, 1"]
                    + self.clear_uavs(),
                )
                self.s(
                    call("Retain"), ["cs = " + path + "/retain.hlsl"]
                    + params(19) + bind + uavs + ["Dispatch = 1, 1, 1"]
                    + self.clear_uavs(),
                )
            if quality_enabled:
                for tag, phase, count in (
                    ("FaceParallel", 14, int(cfg[0, 2])),
                    ("FaceResolve", 15, p),
                ):
                    self.s(
                        call(tag),
                        ["cs = " + path + "/parallel_faces.hlsl"]
                        + params(phase) + bind
                        + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                        + uavs + ["cs-u2 = ref " + r(f"FaceImpulses_s{slot}")]
                        + [f"Dispatch = {(count + 63) // 64}, 1, 1"]
                        + self.clear_uavs(),
                    )
            for index in range(len(offsets) - 1):
                count = int(offsets[index + 1] - offsets[index])
                self.s(
                    call("Face" + str(index)),
                    ["cs = " + path + "/cloth_nodes_p5.hlsl"]
                    + params(5)
                    + [f"y118 = {int(offsets[index])}", f"z118 = {count}"]
                    + bind
                    + ["cs-t76 = null", "cs-t77 = null", "cs-t78 = null"]
                    + uavs
                    + [f"Dispatch = {(count + 63) // 64}, 1, 1"]
                    + self.clear_uavs(),
                )
            for tag, phase, groups in (
                [
                    ("SelfClear", 10, 64),
                    ("SelfHash", 11, (p + 63) // 64),
                    ("Self", 12, (p + 63) // 64),
                ]
                if self.self_collision
                else []
            ):
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
                call("Normals"),
                ["cs = " + path + "/display_normals.hlsl"]
                + bind
                + [
                    "cs-t76 = ref " + r(f"Particles_s{slot}"),
                    "cs-t77 = ref " + r("State"),
                    "cs-u0 = ref " + r(f"DisplayNormals_s{slot}"),
                    f"Dispatch = {groups}, 1, 1",
                    "cs-u0 = null", "cs-t76 = null", "cs-t77 = null",
                ],
            )
            self.s(
                call("Apply"),
                ["cs = " + path + "/apply.hlsl"]
                + bind
                + [
                    "cs-t76 = ref " + r(f"Particles_s{slot}"),
                    "cs-t77 = ref " + r("State"),
                    "cs-t97 = ref " + r(f"Weights_s{slot}"),
                    "cs-t99 = ref " + r(f"FrozenDisplay_s{slot}"),
                    "cs-t74 = ref " + r(f"DisplayNormals_s{slot}"),
                    "cs-u0 = ref " + self.r(f"Write_s{slot}"),
                    f"Dispatch = {(self.meta['vertices'] + 255) // 256}, 1, 1",
                    "cs-u0 = null",
                    "cs-t76 = null",
                    "cs-t77 = null",
                    "cs-t97 = null",
                    "cs-t99 = null",
                    "cs-t74 = null",
                ],
            )
            lines = [
                f"{self.var}passive = 0",
                f"{self.var}repair_g{g} = 0",
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
                    "FrozenMesh",
                    "FrozenDisplay",
                )
            ]
            if cfg[14, 3] == 2:
                lines += ["\tclear = " + r(f"ShortsBinding_s{slot}")]
            lines += [
                f"\t{self.var}boot_g{g}_s{slot} = 1",
                "endif",
                f"w118 = {slot}",
                "run = " + select,
                f"{self.var}control_due_g{g} = 0",
                f"if {self.var}control_time_g{g} != time",
                f"\t{self.var}control_due_g{g} = 1",
                "\t" + run("Control"),
                f"\t{self.var}control_time_g{g} = time",
                "endif",
                run("Frame"),
                run("Startup"),
            ]
            if quality_enabled:
                lines += [run("Precision")]
            lines += [run("Advect")]
            if quality_enabled:
                lines += [
                    f"{self.var}transport_g{g} = 1", run("Validate"), run("Quality"),
                    run("Wake"), f"{self.var}transport_g{g} = 0",
                ]
            lines += [run("Contacts"), f"{self.var}micro_stage = 1"]
            if bands:
                lines += [run("LevelsClear"), run("LevelsFill")]
            for micro in range(3):
                lines += [f"{self.var}micro = {micro}"]
                if cfg.shape[0] <= 16 or cfg[16, 3] < 1.5:
                    lines += [run("HullClear"), run("Hull")]
                lines += [
                    run("Predict"),
                    run("Body"),
                    run("Edges"),
                    run("Body"),
                    run("Velocity"),
                ]
            lines += [f"{self.var}micro_stage = 0"]
            if self.self_collision:
                lines += [
                    r(f"Snapshot_s{slot}") + " = copy " + r(f"Particles_s{slot}"),
                    run("SelfClear"),
                    run("SelfHash"),
                    run("Self"),
                ]
            lines += [
                run("Body"),
                run("Edges"),
                run("Recontact"),
            ]
            scoped = cfg.shape[0] > 16 and cfg[16, 0] > 0.5
            # Keep the material iterations: a geometrically safe intermediate
            # pose does not prove that its stretch constraints have converged.
            # Only repeated contact searches may skip validated-safe geometry.
            for iteration in range(8 if scoped else 2):
                checked_contacts = quality_enabled and iteration >= 2
                if checked_contacts:
                    lines += [run("Validate"), run("Quality"), f"{self.var}repair_g{g} = 1"]
                if scoped:
                    lines += [run("Collision")]
                lines += (
                    [run("FaceParallel"), run("FaceResolve")]
                    if quality_enabled
                    else [run("Face" + str(i)) for i in range(len(offsets) - 1)]
                )
                if checked_contacts:
                    lines += [f"{self.var}repair_g{g} = 0"]
                lines += [run("Edges")]
                if not scoped:
                    lines += [run("Collision")]
            if scoped:
                if quality_enabled:
                    lines += [run("Validate"), run("Quality"), run("Route"), f"{self.var}repair_g{g} = 1"]
                lines += [run("Collision")]
                lines += [run("Face" + str(i)) for i in range(len(offsets) - 1)]
                lines += [run("Collision")]
                if quality_enabled:
                    lines += [f"{self.var}repair_g{g} = 0"]
            if quality_enabled:
                lines += [run("Validate"), run("Quality"), f"{self.var}repair_g{g} = 1"]
                for _ in range(4):
                    for repeat in range(4):
                        lines += [f"{self.var}edge_repeat_g{g} = {repeat}", run("Edges")]
                    lines += [f"{self.var}edge_repeat_g{g} = 0", run("Collision")]
                    lines += [run("Face" + str(i)) for i in range(len(offsets) - 1)]
                    lines += [run("Collision"), run("Validate"), run("Quality")]
                lines += [f"{self.var}repair_g{g} = 0"]
            lines += [run("Velocity"), run("Weights"), run("Normals"), run("Apply")]
            self.s(self.cmd(f"G{g}Simulate_s{slot}"), lines)
            # Stopping the pointer freezes the last solved mesh. This path
            # deliberately contains no collision search or constraint solve.
            self.s(self.cmd(f"G{g}Follow_s{slot}"), [
                f"{self.var}passive = 0", f"w118 = {slot}", "run = " + select, run("Skin"),
                f"{self.var}control_due_g{g} = 0",
                f"if {self.var}control_time_g{g} != time",
                "\t" + run("Control"),
                f"\t{self.var}control_time_g{g} = time", "endif", run("Normals"), run("Apply"),
            ])
            simulate = self.sections["[" + self.cmd(f"G{g}Simulate_s{slot}") + "]"]
            simulate.insert(len(simulate)-2, run("Capture"))
            simulate.append(run("CaptureDisplay"))

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
                    f"Dispatch = {(v + 255) // 256}, 1, 1",
                    "cs-u0 = null",
                    "cs-t111 = null",
                ],
            )
        self.resource("DetectInput", stride=16)
        self.resource("NativeSnapshot", fmt="R32_UINT", stride=4, alias=True)
        self.sections[f"[{self.r('NativeSnapshot')}]"].append("misc_flags = none")
        self.resource("BindSource", stride=stride, file=paths["bind"])
        self.resource("Blend", fmt="R32G32B32A32_FLOAT", file=paths["blend"])
        for index, pool in enumerate(self.bodies):
            prefix = f"B{index}_" if index else ""
            self.resource(
                prefix + "BodyVerts",
                fmt="R32_UINT",
                file=pool["paths"]["body_vertices"],
            )
            self.resource(
                prefix + "BodyOrder", fmt="R32_UINT", file=pool["paths"]["body_order"]
            )
        for name, value in [
            ("slot", 1),
            ("frame_time", -1),
            ("previous_time", -1),
            ("dt", 1 / 60),
            ("micro", 0),
            ("micro_stage", 0),
            ("need_render", 0),
            ("activated", 0),
            ("activation_reset", -1),
            ("solve_until", -1),
            ("passive", 0),
            ("cursor_x", -1),
            ("cursor_y", -1),
            ("mouse_held", 0),
        ]:
            self.global_(name, value)
        for slot in (1, 2):
            self.global_(f"ready_s{slot}", -1)
            self.global_(f"source_valid_s{slot}", 0)
            self.resource(f"SkinSource_s{slot}", stride=stride, alias=True)
            self.resource(
                f"SkinPalette_s{slot}",
                m["palette_rows"][slot],
                "R32G32B32A32_FLOAT",
            )
            self.sections[f"[{self.r(f'SkinPalette_s{slot}')}]"].append(
                "bind_flags = shader_resource"
            )
            self.resource(f"Output_s{slot}", v, stride=stride, vertex=True)
            self.resource(f"SeedWrite_s{slot}", fmt="R32_UINT", stride=4, alias=True)
            self.resource(f"Write_s{slot}", fmt="R32_FLOAT", stride=4, alias=True)
            self.s(
                self.shader(f"Seed_s{slot}"),
                [
                    "cs = " + paths["copy_native_shader"],
                    "cs-t0 = ref " + self.r("NativeSnapshot"),
                    "cs-u0 = ref " + self.r(f"SeedWrite_s{slot}"),
                    f"Dispatch = {(v * stride // 4 + 255) // 256}, 1, 1",
                    "cs-u0 = null",
                    "cs-t0 = null",
                ],
            )
            for name in ("Native", "Published"):
                self.resource(f"{name}_s{slot}", stride=stride, alias=True)
            self.resource(f"NativeBlend_s{slot}", alias=True)
            for index, pool in enumerate(self.bodies):
                prefix = f"B{index}_" if index else ""
                leaves = pool["leaves"]
                body = lambda name, index=index: self.body_name(index, name)
                self.resource(
                    prefix + f"Bounds_s{slot}",
                    leaves * 4,
                    "R32G32B32A32_FLOAT",
                    writable=True,
                )
                self.resource(
                    prefix + f"Exterior_s{slot}",
                    pool["body_count"],
                    "R32_UINT",
                    writable=True,
                )
                self.s(
                    self.shader(f"{prefix}Exterior_s{slot}"),
                    [
                        "cs = " + m["directory"] + "/g0/exterior.hlsl",
                        f"y115 = {leaves}",
                        "cs-t24 = ref " + self.r("NativeInput"),
                        "cs-t87 = ref " + body("BodyVerts"),
                        "cs-t89 = ref " + body("BodyOrder"),
                        "cs-t91 = ref " + body(f"Bounds_s{slot}"),
                        "cs-t106 = null",
                        "cs-u0 = ref " + body(f"Exterior_s{slot}"),
                        f"Dispatch = {(pool['body_count'] + 127) // 128}, 1, 1",
                        "cs-u0 = null",
                    ],
                )
                for tag, phase, groups in [
                    ("Leaves", 0, leaves // 32),
                    ("Roots", 1, max(1, (leaves // 32 + 30) // 32)),
                ]:
                    self.s(
                        self.shader(f"{prefix}{tag}_s{slot}"),
                        [
                            "cs = " + paths["bounds_shader"],
                            "x115 = 1",
                            f"y115 = {leaves}",
                            f"z115 = {phase}",
                            "cs-t24 = ref " + self.r("NativeInput"),
                            "cs-t87 = ref " + body("BodyVerts"),
                            "cs-t89 = ref " + body("BodyOrder"),
                            "cs-t91 = null",
                            "cs-u4 = ref " + body(f"Bounds_s{slot}"),
                            f"Dispatch = {groups}, 1, 1",
                            "cs-u4 = null",
                        ],
                    )
        for g, data in enumerate(m["garments"]):
            self.garment(g, data)
        source = lambda slot: self.r(f"SkinSource_s{slot}")
        enabled = f"{self.enabled} == 1 && {self.mode} >= 2"
        effective = enabled + f" && {self.var}activated == 1"
        lines = [
            "if " + enabled,
            f"\tif {self.var}activation_reset != {self.reset}",
            f"\t\t{self.var}activated = 0",
            f"\t\t{self.var}activation_reset = {self.reset}",
            "\tendif",
            "\tif $isMouseButtonDown == 1",
            f"\t\t{self.var}activated = 1",
            f"\t\tif {self.var}mouse_held == 0 || $cursorX != {self.var}cursor_x || $cursorY != {self.var}cursor_y",
            f"\t\t\t{self.var}solve_until = time",
            "\t\tendif",
            "\tendif",
            f"\t{self.var}cursor_x = $cursorX",
            f"\t{self.var}cursor_y = $cursorY",
            f"\t{self.var}mouse_held = $isMouseButtonDown",
            "endif",
            "if " + effective,
            f"\tif {self.var}frame_time != time",
            f"\t\t{self.var}frame_time = time",
            f"\t\t{self.var}dt = time - {self.var}previous_time",
            f"\t\tif {self.var}previous_time < 0 || {self.var}dt > 0.05 || {self.var}dt <= 0",
            f"\t\t\t{self.var}dt = 0.0166667",
            "\t\tendif",
            f"\t\t{self.var}previous_time = time",
            f"\t\t{self.var}slot = 1",
            f"\t\tif $zz_ms_occ_{m['occ']} == 2",
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
                f"\tif {self.var}slot == {slot} && {self.var}source_valid_s{slot} == 1",
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
                + self.r("NativeSnapshot")
                + " = copy "
                + source(slot)
                + " unless_null",
                "\t\t\t"
                + self.r(f"SeedWrite_s{slot}")
                + " = ref "
                + self.r(f"Output_s{slot}"),
                "\t\t\trun = " + self.shader(f"Seed_s{slot}"),
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
            for index in range(1, len(self.bodies)):
                lines.extend(
                    "\t\t\trun = " + self.shader(f"B{index}_{tag}_s{slot}")
                    for tag in ("Leaves", "Roots")
                )
            for index in range(len(self.bodies)):
                prefix = f"B{index}_" if index else ""
                lines.append("\t\t\trun = " + self.shader(f"{prefix}Exterior_s{slot}"))
            if m["dynamic_bind"] is None:
                insertion = next(
                    i
                    for i, line in enumerate(lines)
                    if line.strip() == "run = " + self.shader(f"Leaves_s{slot}")
                )
                lines.insert(insertion, "\t\t\trun = " + self.shader("CopyBind"))
            body_start = next(
                i for i, line in enumerate(lines)
                if line.strip() == "run = " + self.shader(f"Leaves_s{slot}")
            )
            lines.insert(body_start, f"\t\t\tif time <= {self.var}solve_until")
            for i in range(body_start+1, len(lines)):
                lines[i] = "\t" + lines[i]
            lines.append("\t\t\tendif")
            lines += [f"\t\t\tif time <= {self.var}solve_until"]
            lines += ["\t\t\t\trun = "+self.cmd(f"G{g}Simulate_s{slot}")for g in range(len(m["garments"]))]
            lines += ["\t\t\telse"]
            lines += ["\t\t\t\trun = "+self.cmd(f"G{g}Follow_s{slot}")for g in range(len(m["garments"]))]
            lines += ["\t\t\tendif"]
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
                    f"\tif {self.var}slot == {slot} && {self.var}ready_s{slot} == time",
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
        "surface_gap_mm": 1.0,
        "sleep_enabled": 1,
        "adaptive_quality": 1,
        "level_search": 1,
        "sleep_pose_mm": 2.0,
        "sleep_recheck_s": 0.12,
    }
    for key, default in settings.items():
        value = (
            getattr(node, "cloth_" + key, default)
            if key not in ("enabled", "reset")
            else default
        )
        if isinstance(value, bool):
            value = int(value)
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
            binding = "so0" if vertices == "so0" else "cs-u0"
            inserts[section][index] += [
                indent + DRAW_BEGIN,
                indent + emitter.var + f"source_valid_s{slot} = 0",
                indent + f"if {binding} !== null",
                indent + "\t" + emitter.r(f"SkinSource_s{slot}") + f" = ref {binding}",
                indent
                + "\t"
                + emitter.r(f"SkinPalette_s{slot}")
                + f" = copy {palette}",
                indent + "\t" + emitter.var + f"ready_s{slot} = -1",
                indent + "\t" + emitter.var + f"source_valid_s{slot} = 1",
                indent + "endif",
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
