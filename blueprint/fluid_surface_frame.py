"""Read the exported rest-pose shading frame used by the native ZZMI VS."""
from pathlib import Path
import math
import re
import struct

from .fluid_ini_merge import parse_sections, scan_mesh_draws, resolve_first_index, locate_draw_line


def exported_triangle_rows(mod_path, emitter):
    """Return three POSITION/NORMAL/TANGENT rows, or None for another layout."""
    root = Path(mod_path)
    match = re.search(r'(?:LOD\d+\.)?([0-9a-f]{8})-', emitter.object_name, re.I)
    if not match:
        return None
    for ini in sorted(root.glob('*.ini')):
        try:
            text = ini.read_text('utf-8-sig')
        except (OSError, UnicodeError):
            continue
        first = resolve_first_index(emitter.poly_index, emitter.object_name,
                                    emitter.first_index, emitter.ib_file, scan_mesh_draws(text))
        hit = locate_draw_line(text, emitter.object_name, first) if first is not None else None
        if hit is None:
            continue
        prefix = text[:hit[0]]
        headers = list(re.finditer(r'(?m)^\s*\[[^\]\r\n]+\][^\r\n]*$', prefix))
        scope = prefix[headers[-1].end():] if headers else ''
        bindings = list(re.finditer(r'(?mi)^\s*ib\s*=\s*(?:ref\s+)?(Resource[^\s;]+)', scope))
        if not bindings:
            continue
        sections = {key.casefold(): value for key, value in parse_sections(text).items()}

        def settings(name):
            return {line.split('=', 1)[0].strip().casefold(): line.split('=', 1)[1].split(';', 1)[0].strip()
                    for line in sections.get(name.casefold(), []) if '=' in line}

        ib = settings(bindings[-1][1])
        vb = settings('Resource'+match[1]+'Position')
        if vb.get('stride') != '40' or 'filename' not in vb or 'filename' not in ib:
            continue
        width = {'R16_UINT': 2, 'R32_UINT': 4}.get(ib.get('format', '').upper().removeprefix('DXGI_FORMAT_'))
        if width is None:
            continue
        index_path, vertex_path = root / ib['filename'], root / vb['filename']
        if not index_path.is_file() or not vertex_path.is_file():
            continue
        try:
            with index_path.open('rb') as f:
                f.seek(first*width)
                blob = f.read(3*width)
        except OSError:
            continue
        if len(blob) != 3*width:
            continue
        indices = struct.unpack('<'+('H' if width == 2 else 'I')*3, blob)
        indices = [index+int(getattr(emitter, 'base_vertex', 0)) for index in indices]
        if min(indices) < 0:
            continue
        rows = []
        try:
            with vertex_path.open('rb') as f:
                for index in indices:
                    f.seek(index*40)
                    blob = f.read(40)
                    if len(blob) != 40:
                        return None
                    row = struct.unpack('<10f', blob)
                    if not all(math.isfinite(value) for value in row):
                        return None
                    rows.append(row)
        except OSError:
            continue
        return rows
    return None
