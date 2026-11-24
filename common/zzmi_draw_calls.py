"""Identify indexed draws and the generated material calls that replace them."""

import re

from .ttl_protocol import parse_ttl_draw_args

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*(?:;.*)?$")
_DRAW = re.compile(r"^\s*drawindexed(instanced)?\s*=\s*([\d, \t-]+)\s*$", re.I)
_RUN = re.compile(r"^\s*run\s*=\s*(\S+)\s*$", re.I)
_TTL_DRAW = r"commandlist\ttl\draw"
_MATERIAL_DRAW = "; --- Start of Overridden Mesh Content ---"
_BONE_TTL_DRAW = rf"; ssmt bone matrix begin draw|run = {_TTL_DRAW}"
DRAW_RANGE_MARKER = "; SSMT draw range "
TEAR_DRAW_PREFIX = "commandlistdragtearnativettl"


def _direct_range(line):
    match = _DRAW.fullmatch(line)
    if not match:
        return None
    try:
        args = [int(value) for value in match[2].split(",")]
    except ValueError:
        return None
    start = 2 if match[1] else 1
    if len(args) <= start + 1 or args[0] <= 0 or args[start] < 0:
        return None
    return args[0], args[start], args[start + 1]


def indexed_draw_ranges(lines):
    """Map command line numbers to (index count, first index, base vertex)."""
    sections = {}
    body = []
    for line in lines:
        match = _SECTION.fullmatch(line)
        if match:
            body = sections.setdefault(match[1].casefold(), [])
        else:
            body.append(line)
    material = {}
    for name, body in sections.items():
        if name.startswith(TEAR_DRAW_PREFIX):
            for line in body:
                line = line.strip()
                if line.startswith(DRAW_RANGE_MARKER):
                    draw = _direct_range("drawindexed = " + line[len(DRAW_RANGE_MARKER):])
                    if draw:
                        material[name] = draw
                    break
        if _MATERIAL_DRAW not in (line.strip() for line in body):
            continue
        draws = [draw for line in body if (draw := _direct_range(line))]
        if len(draws) == 1 and not any(
            re.match(r"\s*(?:vb0|ib)\s*=", line, re.I) for line in body
        ):
            material[name] = draws[0]

    ranges = {}
    wrapped_ttl = None
    section_start = 0
    for index, line in enumerate(lines):
        if _SECTION.fullmatch(line):
            section_start = index + 1
            wrapped_ttl = None
        elif draw := _direct_range(line):
            ranges[index] = draw
        elif line.strip().casefold() == _BONE_TTL_DRAW:
            wrapped_ttl = parse_ttl_draw_args(lines, index, section_start)
        elif match := _RUN.fullmatch(line):
            target = match[1].casefold()
            if target == _TTL_DRAW:
                if args := wrapped_ttl or parse_ttl_draw_args(lines, index, section_start):
                    ranges[index] = args
                wrapped_ttl = None
            elif target in material:
                ranges[index] = material[target]
    return ranges
