
"""Publish isolated ZZMI LOD exports using the standard Mod layout."""
from collections import defaultdict
import hashlib
from pathlib import Path
import re

from . import safe_write


HEADER = re.compile(r"(?m)^\[([^\]\r\n]+)\][ \t]*$")
VARIABLE = re.compile(r"\$([a-zA-Z_][a-zA-Z0-9_]*)")
FILENAME = re.compile(r"(?i)^(\s*(?:filename|[vcpgdhs]s)\s*=\s*)([^\r\n;]+)")


def _ini_sections(path):
    text = path.read_text(encoding="utf-8-sig")
    headers = list(HEADER.finditer(text))
    return [(m[1], text[m.end():headers[i + 1].start() if i + 1 < len(headers) else len(text)])
            for i, m in enumerate(headers)]


def _asset_targets(staged, destination):
    claims = {}
    mappings = {}
    for lod, _, _, folder in staged:
        mapping = {}
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if not path.is_file() or len(relative.parts) < 2:
                continue
            if relative.parts[0] not in {"Textures", "res"} and not re.fullmatch(r"Meshes\d*", relative.parts[0]):
                continue
            data = path.read_bytes()
            digest = hashlib.sha256(data).digest()
            target = relative
            key = target.as_posix().casefold()
            if key in claims and claims[key] != digest:
                target = relative.with_name(f"{lod}_{relative.name}")
                key = target.as_posix().casefold()
            if key in claims and claims[key] != digest:
                raise ValueError(f"不同 LOD 使用了同一个文件名：{target}")
            if key not in claims:
                safe_write.write_bytes_if_changed_atomic(str(destination / target), data)
                claims[key] = digest
            mapping[relative.as_posix().casefold()] = target.as_posix()
        mappings[lod] = mapping
    return mappings


def publish_lod_exports(exports, main_ini, buffers_only=False):
    main_ini = Path(main_ini)
    staged = [(lod, model, exporter, Path(path)) for lod, model, exporter, path in exports]
    files = _asset_targets(staged, main_ini.parent)
    if buffers_only:
        return

    common = defaultdict(list)
    body = []
    shader_signatures = set()
    for lod, _, _, folder in staged:
        sections = [item for path in sorted(folder.glob("*.ini")) for item in _ini_sections(path)]
        names = {}
        shader_groups = defaultdict(list)
        for name, lines in sections:
            key = name.casefold()
            if key in {"constants", "present"}:
                continue
            if key.startswith("shaderregex"):
                base, period, suffix = name.partition(".")
                signature = "\n".join(line for line in lines.strip().splitlines() if not line.startswith(";"))
                shader_groups[base].append((suffix, signature))
                names[key] = f"{base}_{lod}{period}{suffix}"
            else:
                names[key] = f"{name}_{lod}"
        skip_shader = {base for base, group in shader_groups.items() if tuple(group) in shader_signatures}
        shader_signatures.update(tuple(group) for group in shader_groups.values())
        references = re.compile(
            r"(?<![\w\\$])(?:" + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
            + r")(?![\w\\.-])", re.IGNORECASE,
        ) if names else None

        def rewrite_file(match):
            path = match[2].strip().replace("\\", "/").removeprefix("./")
            destination = files[lod].get(path.casefold())
            return match[1] + destination if destination else match[0]

        for name, lines in sections:
            key = name.casefold()
            if key.startswith("shaderregex"):
                if name.partition(".")[0] not in skip_shader:
                    body.append(f"[{names[key]}]{lines.rstrip()}\n\n")
                continue
            rewritten = []
            for line in lines.splitlines(keepends=True):
                if not line.lstrip().startswith(";"):
                    line = FILENAME.sub(rewrite_file, line)
                    if references is not None:
                        line = references.sub(lambda m: names[m[0].casefold()], line)
                    line = VARIABLE.sub(lambda m: "$" + names.get(m[1].casefold(), lod.lower() + "_" + m[1]), line)
                rewritten.append(line)
            text = "\n" + "".join(rewritten).strip("\r\n") + "\n\n"
            if key in {"constants", "present"}:
                common[key].append(text)
            else:
                body.append(f"[{names[key]}]{text}")
    output = [";MARK:TextureOverrideIB\n"]
    output.extend(f"[{name.title()}]\n" + "".join(lines) for name, lines in common.items())
    output.extend(body)
    safe_write.write_bytes_if_changed_atomic(str(main_ini), "".join(output).encode("utf-8"))
