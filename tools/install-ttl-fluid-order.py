"""Install the TTL fluid handoff and update an existing fluid export.

Default: check only. Use --apply to install; originals are backed up together.
The source Probe.ini already carries the same hook for subsequent exports.
"""

import argparse
import re
import shutil
from datetime import datetime
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "Toolset/fluid/TTLibFluidOrder.ini"
DEFAULT_TTL = Path(r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Core\ZZMI\Libraries\TTLib")
INCLUDE = "[Include TTLibFluidOrder]\ninclude = FluidOrder.ini\n"
HOOK = (
    r"    Resource\TTL\FluidOrderTarget = ref ResourceSceneColor" + "\n"
    r"    run = CommandList\TTL\ApplyBeforeFluid" + "\n"
)


def edited_bytes(raw, edit):
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    changed = edit(text.replace("\r\n", "\n"))
    return (("\ufeff" if raw.startswith(b"\xef\xbb\xbf") else "")
            + changed.replace("\n", newline)).encode("utf-8")


def with_include(text):
    if re.search(r"(?im)^\s*include\s*=\s*FluidOrder\.ini\s*$", text):
        return text
    return text.rstrip("\n") + "\n\n" + INCLUDE


def with_hook(text):
    if r"run = CommandList\TTL\ApplyBeforeFluid" in text:
        return text
    pattern = re.compile(r"(?m)^([ \t]*)ResourceFluidLighting = copy ResourceSceneColor$")
    if len(pattern.findall(text)) != 1:
        raise ValueError("Expected one fluid background snapshot in the exported INI")
    return pattern.sub(lambda m: HOOK + m[0], text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ttl-dir", type=Path, default=DEFAULT_TTL)
    parser.add_argument("--mod-dir", type=Path, help="Export directory containing fluid_probe.ini")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    core = (args.ttl_dir / "TTLib.ini").read_text(encoding="utf-8-sig")
    for section in ("CommandListApplyLateWorldComposite", "ResourceWorldRTFlip"):
        if "[" + section + "]" not in core:
            raise ValueError("Unsupported TTLib: missing " + section)

    includes = args.ttl_dir / "Includes.ini"
    planned = {args.ttl_dir / "FluidOrder.ini": SOURCE.read_bytes(),
               includes: edited_bytes(includes.read_bytes(), with_include)}
    if args.mod_dir:
        mod_ini = args.mod_dir / "fluid_probe.ini"
        planned[mod_ini] = edited_bytes(mod_ini.read_bytes(), with_hook)
    changes = {path: content for path, content in planned.items()
               if not path.exists() or path.read_bytes() != content}
    for path in changes:
        print("UPDATE:", path)
    if not changes:
        print("OK: TTL/fluid ordering is installed")
        return 0
    if not args.apply:
        print("CHECK: use --apply to install")
        return 1

    backup = (args.ttl_dir.parents[3] / "_mod-backups" /
              ("ttl-fluid-order-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")))
    backup.mkdir(parents=True)
    originals = {path: path.read_bytes() if path.exists() else None for path in changes}
    for index, path in enumerate(changes):
        if path.exists():
            shutil.copy2(path, backup / (str(index) + "-" + path.name + ".bak"))
    (backup / "paths.txt").write_text(
        "\n".join(str(path) for path in changes) + "\n", encoding="utf-8")
    written = []
    try:
        for path, content in changes.items():
            if (path.read_bytes() if path.exists() else None) != originals[path]:
                raise RuntimeError("File changed during install: " + str(path))
            path.write_bytes(content)
            written.append(path)
    except Exception:
        for path in reversed(written):
            if path.read_bytes() != changes[path]:
                print("Concurrent edit preserved:", path)
                continue
            content = originals[path]
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise
    print("BACKUP:", backup)
    print("OK: installed TTL -> fluid background -> fluid composite")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
