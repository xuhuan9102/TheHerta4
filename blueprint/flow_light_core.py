"""模型流光的参数、宏注入及 mesh 绘制解析；不依赖 bpy。"""

import re

DEFAULT_PARAMS = {'scan_speed': 0.0405,
 'phase': 0.0,
 'direction': 1.0,
 'intensity': 2.6,
 'core_width': 0.024,
 'halo_width': 0.095,
 'core_gain': 2.2,
 'halo_gain': 0.18,
 'base_gain': 0.08,
 'hue_speed': 0.45,
 'hue_bands': 6.0,
 'emission_base': 1.3,
 'emission_gain': 0.8,
 'emission_cap': 4.0,
 'core_white_mix': 0.48}

_DEFINE_RE = re.compile(r"^#define (FL_[A-Z0-9_]+)[ \t]+[^\r\n]*", re.MULTILINE)


def shader_constants(params=None):
    return {"FL_" + key.upper(): float((params or {}).get(key, value))
            for key, value in DEFAULT_PARAMS.items()}


def format_constant_value(value):
    """整数浮点去掉 .0（`FL_MASK_W 1920` 这种宏必须能 round-trip），其余用 repr 保精度。"""
    text = repr(float(value))
    if text.endswith(".0"):
        text = text[:-2]
    return text


def inject_shader_constants(text, constants):
    if not text:
        return text or ""
    values = {}
    for name, value in (constants or {}).items():
        name = str(name)
        if name.startswith("FL_"):
            values[name] = format_constant_value(value)

    def _replace(match):
        name = match.group(1)
        if name not in values:
            return match.group(0)
        return "#define %s %s" % (name, values[name])

    return _DEFINE_RE.sub(_replace, text)


def _normalize_section_key(section_name):
    normalized = str(section_name or "").strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1].strip()
    return normalized.casefold()


def _extract_assignment_value(line, key_name):
    stripped = str(line or "").strip()
    if "=" not in stripped:
        return ""
    name, value = stripped.split("=", 1)
    if name.strip().casefold() != str(key_name or "").strip().casefold():
        return ""
    return value.strip()


def _extract_run_target(line):
    target = _extract_assignment_value(line, "run")
    if target.startswith("[") and target.endswith("]"):
        target = target[1:-1].strip()
    return target


def _parse_draw_command(line):
    stripped = str(line or "").strip()
    if "=" not in stripped:
        return None
    name, raw_params = stripped.split("=", 1)
    name = name.strip().casefold()
    parts = [part.strip() for part in raw_params.strip().split(",")]

    def as_int(text):
        text = str(text or "").strip()
        return int(text) if text.lstrip("-").isdigit() else None

    if name == "drawindexed" and len(parts) == 3:
        params = tuple(as_int(part) for part in parts)
        if all(value is not None for value in params):
            return "drawindexed", params
    elif name == "draw" and len(parts) == 2:
        params = tuple(as_int(part) for part in parts)
        if all(value is not None for value in params):
            return "draw", params
    return None


def _resolve_draw_in_section(sections, lookup, section_name, run_path=None, visited=None):
    if not section_name:
        return None
    visited = set(visited or ())
    if section_name in visited:
        return None
    visited.add(section_name)
    lines = sections.get(section_name) or []
    current_run_path = list(run_path or [])
    for line_index, line in enumerate(lines):
        parsed = _parse_draw_command(line)
        if parsed is not None:
            return {
                "draw_type": parsed[0],
                "draw_params": parsed[1],
                "draw_section_name": section_name,
                "line_index": line_index,
                "run_path": current_run_path,
            }
        target = _extract_run_target(line)
        if not target:
            continue
        target_section = lookup.get(_normalize_section_key(target), "")
        if not target_section:
            continue
        result = _resolve_draw_in_section(
            sections, lookup, target_section,
            run_path=current_run_path + [(section_name, line_index)],
            visited=visited,
        )
        if result is not None:
            return result
    return None


def _resolve_draw_in_mesh_block(sections, lookup, section_name, lines, mesh_line_index, block_end):
    for line_index in range(mesh_line_index + 1, block_end):
        parsed = _parse_draw_command(lines[line_index])
        if parsed is not None:
            return {
                "draw_type": parsed[0],
                "draw_params": parsed[1],
                "draw_section_name": section_name,
                "line_index": line_index,
                "run_path": [],
            }
        target = _extract_run_target(lines[line_index])
        if not target:
            continue
        target_section = lookup.get(_normalize_section_key(target), "")
        if not target_section:
            continue
        result = _resolve_draw_in_section(
            sections, lookup, target_section,
            run_path=[(section_name, line_index)],
            visited={section_name},
        )
        if result is not None:
            return result
    return None


def parse_draw_targets(sections):
    """按 uv offset 节点同一套规则解析 [TextureOverride*] 里的 [mesh:...] 绘制块。"""
    if not sections:
        return []
    lookup = {}
    for section_name in sections.keys():
        key = _normalize_section_key(section_name)
        if key and key not in lookup:
            lookup[key] = section_name

    targets = []
    for section_name, raw_lines in sections.items():
        if not str(section_name).strip().lower().startswith("[textureoverride"):
            continue
        lines = list(raw_lines or [])
        for mesh_line_index, line in enumerate(lines):
            mesh_match = re.search(r"\[mesh:([^\]]+)\]", str(line).strip())
            if not mesh_match:
                continue
            mesh_name = mesh_match.group(1).strip()
            block_end = len(lines)
            for next_index in range(mesh_line_index + 1, len(lines)):
                if re.search(r"\[mesh:([^\]]+)\]", str(lines[next_index])):
                    block_end = next_index
                    break
            resolved = _resolve_draw_in_mesh_block(
                sections, lookup, section_name, lines, mesh_line_index, block_end,
            )
            if resolved is None:
                continue
            targets.append({
                "section_name": section_name,
                "line_index": resolved["line_index"],
                "mesh_name": mesh_name,
                "draw_type": resolved["draw_type"],
                "draw_params": resolved["draw_params"],
                "run_path": resolved["run_path"],
            })
    return targets


