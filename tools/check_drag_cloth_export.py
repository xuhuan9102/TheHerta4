"""Audit exported INI/resources without Blender; optionally compile active cloth CS.

    python tools/check_drag_cloth_export.py MOD --compile --report audit.json

This checks the generated package contract. It does not certify collision-free
geometry or replace an in-game test or a replay of the generated command lists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _audit_proxy(root, sections, section_commands, require, component, group, vertex_count):
    prefix = f"{component}_G{group}_"
    if prefix + "ProxyTrack" not in sections and prefix + "ProxyRows" not in sections:
        return {}
    rows = np.fromfile(root / require(prefix + "ProxyRows", format="R32_UINT")["filename"], "<u4")
    if not len(rows) or rows.max() >= vertex_count or len(np.unique(rows)) != len(rows):
        raise ValueError(f"Invalid proxy display rows: {prefix}")
    track = np.fromfile(root / require(prefix + "ProxyTrack", format="R32G32B32A32_FLOAT")["filename"], "<f4")
    entries = len(rows) * 16
    samples, remainder = divmod(len(track), entries)
    if remainder or samples < 2:
        raise ValueError(f"Invalid proxy track size: {prefix}")
    track = track.reshape(samples, len(rows), 4, 4)
    skin_weights = track[:, :, 3]
    if (not np.isfinite(track[:, :, :2]).all() or not np.isfinite(skin_weights).all()
            or np.any(skin_weights < 0) or not np.allclose(skin_weights.sum(axis=2), 1, atol=2e-5)):
        raise ValueError(f"Invalid proxy track coordinates or weights: {prefix}")
    bones = track[:, :, 2].view("<u4")
    count = min(int(require(f"{component}_SkinPalette_s{slot}")["array"]) for slot in (1, 2)) // 3
    if np.any(bones >= count):
        raise ValueError(f"Proxy track uses bones outside the skin palette: {prefix}")
    component_suffix = component.removeprefix("ResourceSSMTCloth_")
    for slot in (1, 2):
        apply = f"CustomShaderSSMTClothG{group}Apply_s{slot}_{component_suffix}"
        fields = require(apply, dispatch=f"{(len(rows) + 127) // 128}, 1, 1")
        bindings = {f"cs-t28 = ref {prefix}ProxyRows", f"cs-t29 = ref {prefix}ProxyTrack"}
        if not bindings.issubset(section_commands[apply]):
            raise ValueError(f"Proxy track shader missing input binding: {apply}")
        shader = (root / fields["cs"]).read_text("utf-8")
        if not re.search(rf"(?m)^#define PROXY_SAMPLES {samples}\s*$", shader):
            raise ValueError(f"Proxy track sample count disagrees with its shader: {prefix}")
    return {"proxy_rows": len(rows), "proxy_samples": samples}


def audit(root, *, compile_shaders=False, cloth_only=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"Mod directory does not exist: {root}")
    sections, commands, shaders, section_commands = {}, [], set(), {}
    inis = [p for p in root.rglob("*.ini") if "Backups" not in p.relative_to(root).parts]
    if not inis:
        raise ValueError("No exported INI found")
    files = 0
    for ini in sorted(inis):
        current = None
        text = ini.read_text("utf-8-sig")
        if cloth_only and not re.search(r"(?m)^\[(?:Resource|CustomShader)SSMTCloth", text):
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith(";"):
                continue
            if re.fullmatch(r"\[.+\]", line):
                current = line[1:-1]
                if current in sections and current.casefold() not in {"constants", "present"}:
                    raise ValueError(f"Duplicate INI section: {current}")
                sections.setdefault(current, {})
                section_commands.setdefault(current, [])
                continue
            if current is None or "=" not in line:
                continue
            key, value = (v.strip() for v in line.split("=", 1))
            sections[current][key.lower()] = value
            commands.append(line)
            section_commands[current].append(line)
            if key.lower() not in ("filename", "cs") or value.lower() == "null":
                continue
            if cloth_only and not re.match(r"(?:Resource|CustomShader)(?:SSMTCloth|Drag)", current):
                continue
            path = Path(value.strip('"'))
            path = path if path.is_absolute() else ini.parent / path
            path = path.resolve()
            if not path.is_file():
                raise ValueError(f"Missing {key} file: {path}")
            files += 1
            if key.lower() == "cs" and "drag_cloth" in path.parts:
                shaders.add(path)

    assets = 0
    for manifest in (root / "res/drag_cloth").glob("*/manifest.json"):
        data = json.loads(manifest.read_text("utf8"))
        for name, wanted in data["files"].items():
            path = (manifest.parent / name).resolve()
            if not path.is_relative_to(manifest.parent.resolve()):
                raise ValueError(f"Manifest path escapes its directory: {name}")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != wanted:
                raise ValueError(f"Manifest hash mismatch: {path}")
            assets += 1
    command_text = "\n".join(commands)
    declared = {name.casefold() for name in sections}
    cloth_references = set()
    for line in commands:
        if re.match(r"(?:filename|cs)\s*=", line, re.IGNORECASE):
            continue
        cloth_references.update(re.findall(r"\b(?:Resource|CommandList|CustomShader)SSMTCloth\w+", line))
    undefined = sorted(ref for ref in cloth_references if ref.casefold() not in declared)
    if undefined:
        raise ValueError("Undefined cloth reference: " + ", ".join(undefined))

    def require(name, **fields):
        if name not in sections:
            raise ValueError(f"Missing resource: {name}")
        for field, wanted in fields.items():
            if sections[name].get(field) != str(wanted):
                raise ValueError(f"Resource {name}: expected {field} = {wanted}")
        return sections[name]

    garments, components, shorts = [], set(), 0
    for name, fields in sections.items():
        if not name.startswith("ResourceDragJiggleTempVB0_"):
            continue
        require(name, type="Buffer", bind_flags="unordered_access vertex_buffer", misc_flags="none")
        suffix = name.removeprefix("ResourceDragJiggleTempVB0_")
        visible = int(fields.get("array", "0"))
        if visible <= 0:
            raise ValueError("Jiggle draw buffer must declare its seeded visible capacity: " + name)
        native = "ResourceDragJiggleNative_" + suffix
        raw = require(native, type="Buffer", stride=4, format="R32_UINT", misc_flags="none")
        if any(field in raw for field in ("array", "byte_width", "filename")):
            raise ValueError("Jiggle native snapshot must inherit native capacity: " + native)
        seed_writer = "ResourceDragJiggleSeedWrite_" + suffix
        require(seed_writer, stride=4, format="R32_UINT")
        seed = "CustomShaderDragSeed" + suffix
        require(seed, x115=visible*int(fields["stride"])//4)
        for binding in ("cs-t0 = ref " + native, "cs-u0 = ref " + seed_writer):
            if binding not in section_commands[seed]:
                raise ValueError("Jiggle seed missing binding: " + binding)
        solved = "ResourceDragJiggleSolved_" + suffix
        writer = "ResourceDragJiggleWrite_" + suffix
        require(solved, type="RWStructuredBuffer", stride=fields["stride"], array=visible, bind_flags="shader_resource unordered_access")
        require(writer, stride=4, format="R32_FLOAT")
        publisher = "CustomShaderDragPublish" + suffix
        require(publisher)
        for binding in ("cs-t24 = ref " + solved, "cs-u0 = ref " + writer):
            if binding not in section_commands[publisher]:
                raise ValueError(f"Jiggle publisher missing binding: {binding}")
        if not re.search(rf"(?im)^{re.escape(writer)}\s*=\s*ref\s+{re.escape(name)}\s*$", command_text):
            raise ValueError("Jiggle publisher must write the draw buffer: " + name)
    for name, fields in list(sections.items()):
        match = re.fullmatch(r"(ResourceSSMTCloth_.+)_G(\d+)_Config", name)
        if not match:
            continue
        component, group = match.groups()
        prefix = f"{component}_G{group}_"
        path = Path(fields["filename"])
        path = path if path.is_absolute() else root / path
        config = np.fromfile(path, "<f4").reshape(-1, 4)
        if len(config) < 16 or not np.isfinite(config).all():
            raise ValueError(f"Invalid garment config: {name}")
        p, v = (int(x) for x in config[0, :2])
        if not (0 < p <= 2450 and v >= p):
            raise ValueError(f"Invalid proxy/display counts: {name}")
        mix = require(prefix + "SkinMix", format="R32_FLOAT")
        mix_path = Path(mix["filename"])
        mix_path = mix_path if mix_path.is_absolute() else root / mix_path
        values = np.fromfile(mix_path, "<f4")
        if len(values) != p or not np.isfinite(values).all() or np.any((values < 0) | (values > 1)):
            raise ValueError(f"Invalid leg mix field: {prefix}")
        sliding = bool(config[14, 3] == 2)
        shorts += int(sliding)
        for slot in (1, 2):
            for tag, count in (("FrozenMesh", p), ("FrozenDisplay", v)):
                require(prefix + f"{tag}_s{slot}", array=count, format="R32G32B32A32_FLOAT")
            if sliding:
                require(prefix + f"ShortsBinding_s{slot}", array=p*17, format="R32G32B32A32_FLOAT")
            for tag in ("Particles", "Weights"):
                require(prefix + f"{tag}_s{slot}", array=p, stride=32, type="RWStructuredBuffer")
        proxy = _audit_proxy(root, sections, section_commands, require, component, group, v)
        garments.append(dict(group=int(group), particles=p, vertices=v, shorts=sliding, **proxy))
        if component in components:
            continue
        components.add(component)
        for slot in (1, 2):
            output = component + f"_Output_s{slot}"
            fields = require(output, array=v, bind_flags="unordered_access vertex_buffer", misc_flags="none")
            stride = int(fields["stride"])
            require(component + "_NativeInput", type="StructuredBuffer", stride=stride)
            require(component + "_DetectInput", type="StructuredBuffer", stride=16)
            for tag, fmt in (("Write", "R32_FLOAT"), ("SeedWrite", "R32_UINT")):
                alias = component + f"_{tag}_s{slot}"
                require(alias, stride=4, format=fmt)
                pattern = rf"(?im)^{re.escape(alias)}\s*=\s*(\w+)\s+{re.escape(output)}\s*$"
                modes = re.findall(pattern, command_text)
                assignments = re.findall(rf"(?im)^{re.escape(alias)}\s*=\s*(\w+)\b", command_text)
                if not modes or any(mode.lower() != "ref" for mode in assignments):
                    raise ValueError(f"Output alias must reference the draw buffer: {alias}")
    if garments and not assets:
        raise ValueError("Cloth resources have no ownership manifest")
    compiled = 0
    if compile_shaders:
        if sys.platform != "win32":
            raise ValueError("D3D11 shader compilation requires Windows")
        from tools.check_drag_cloth import GPU

        gpu = GPU()
        try:
            for path in sorted(shaders):
                gpu.shader(path)
                compiled += 1
        finally:
            gpu.release()
    return dict(
        passed=True, ini_files=len(inis), referenced_files=files,
        owned_assets=assets, components=len(components), garments=garments,
        cloth_references=len(cloth_references),
        shorts=shorts, active_cloth_shaders=len(shaders), compiled_shaders=compiled,
        geometry_certified=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mod", type=Path)
    parser.add_argument("--compile", action="store_true", dest="compile_shaders")
    parser.add_argument("--cloth-only", action="store_true", help="Check cloth and drag paths in the host INI")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.mod, compile_shaders=args.compile_shaders, cloth_only=args.cloth_only)
    except (ValueError, OSError, KeyError, RuntimeError) as error:
        result = dict(passed=False, error=str(error))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n", "utf8")
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
