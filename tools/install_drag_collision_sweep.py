"""Install the live collision sweep into an exported ZZMI mod, with backups.

Dry run by default; --apply also precompiles the changed shaders before writing.
"""

from __future__ import annotations

import argparse
import ctypes as ct
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import re
import shutil
import hashlib

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sections(text):
    matches = list(re.finditer(r"(?m)^\[([^\]\r\n]+)\][ \t]*$", text))
    return {m.group(1): (m.start(), matches[i + 1].start() if i + 1 < len(matches) else len(text))
            for i, m in enumerate(matches)}


def value(block, key):
    found = re.search(rf"(?mi)^\s*{re.escape(key)}\s*=\s*([^;\r\n]+)", block)
    if not found:
        raise ValueError(f"Missing {key}")
    return found.group(1).strip()


def replace_section(text, name, body):
    index = sections(text)
    block = f"[{name}]\n" + "\n".join(body) + "\n\n"
    if name in index:
        begin, end = index[name]
        return text[:begin] + block + text[end:]
    return text.rstrip() + "\n\n" + block


def inside(root, path):
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path escapes the mod directory: {path}")
    return path


def prepare(mod, ini):
    baker = load("sweep_installer_baker", ROOT / "blueprint/drag_collision.py")
    original = ini.read_bytes()
    bom = original.startswith(b"\xef\xbb\xbf")
    text = original.decode("utf-8-sig").replace("\r\n", "\n")
    payloads, shaders, info = {}, {}, []
    for name in list(sections(text)):
        if not name.startswith("CustomShaderDragJiggle") or "Cursor" in name:
            continue
        index = sections(text)
        begin, end = index[name]
        block = text[begin:end]
        if not re.search(r"(?m)^x101\s*=\s*1\s*$", block):
            continue
        suffix = name.removeprefix("CustomShaderDragJiggle")
        tris_resource = value(block, "cs-t84")
        tri_begin, tri_end = index[tris_resource]
        tri_file = inside(mod, mod / value(text[tri_begin:tri_end], "filename"))
        triangles = np.fromfile(tri_file, dtype="<f4").reshape(-1, 4, 4)
        order, leaves = baker.bake_sweep_order(triangles)
        if not leaves:
            continue
        order_file = tri_file.with_name(tri_file.name.replace("ColliderTriGridTris.buf", "ColliderSweepOrder.buf"))
        if order_file == tri_file:
            raise ValueError("Unsupported collider triangle filename")
        payloads[order_file] = order.tobytes()
        l0_name = value(block, "cs-t79")
        l0_begin, l0_end = index[l0_name]
        l0_file = inside(mod, mod / value(text[l0_begin:l0_end], "filename"))
        contact = np.fromfile(l0_file, dtype="<f4").reshape(-1, 2, 4)
        ibs = {}
        for draw_name, (draw_begin, draw_end) in index.items():
            draw = text[draw_begin:draw_end]
            if not re.search(r"(?m)^\s*run\s*=\s*" + re.escape(name) + r"\s*$", draw):
                continue
            ib = value(draw, "ib")
            ib_begin, ib_end = index[ib]
            path = inside(mod, mod / value(text[ib_begin:ib_end], "filename"))
            bases = {int(base) for count, base in re.findall(r"(?mi)^\s*drawindexed\s*=\s*(\d+)\s*,\s*\d+\s*,\s*(\d+)", draw)
                     if int(count) > 3}
            if len(bases) > 1:
                raise ValueError("Mixed vertex bases require exporter-side cloth baking")
            base = next(iter(bases), 0)
            ibs[path] = base
        if not ibs:
            raise ValueError("Cannot locate the drawn cloth IB")
        all_faces = np.concatenate([np.fromfile(path, dtype="<u4").reshape(-1, 3).astype(np.int64) + base
                                    for path, base in ibs.items()])
        cloth, adjacency = baker.bake_cloth_adjacency(all_faces, contact[:, 1, 3] > .5)
        if not len(cloth):
            raise ValueError("No controlled cloth faces found")
        cloth_file = tri_file.with_name(tri_file.name.replace("ColliderTriGridTris.buf", "ColliderCloth.buf"))
        adjacency_file = tri_file.with_name(tri_file.name.replace("ColliderTriGridTris.buf", "ColliderAdjacency.buf"))
        payloads[cloth_file] = cloth.tobytes()
        payloads[adjacency_file] = adjacency.tobytes()
        dispatch = int(value(block, "Dispatch").split(",")[0])
        shader_rel = value(block, "cs")
        shader = inside(mod, mod / shader_rel)
        main = shader.read_text("utf-8-sig").replace("\r\n", "\n")
        switch = "COLLISION_SURFACE" if "#define COLLISION_SURFACE" in main else (
            "COLLISION_SWEEP" if "#define COLLISION_SWEEP" in main else "COLLISION_SURFACE")
        if f"#define {switch}" not in main:
            main = main.replace("#define COLLISION_META      IniParams[104]",
                                f"#define COLLISION_META      IniParams[104]\n#define {switch}     IniParams[115]")
        main = re.sub(r"#define LINA_PROBE_MODE\s+\d+", "#define LINA_PROBE_MODE 0", main)
        old = "if (COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0 && collisionDue)"
        new = f"if (COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0 && collisionDue && {switch}.x < 0.5)"
        main = main.replace(old, new)
        if new not in main or f"#define {switch}" not in main:
            raise ValueError("Unsupported jiggle shader; refusing to install without bypassing legacy collision")
        shaders[shader] = main
        vertex_struct = re.search(r"struct VertexAttributes\s*\{[^}]*\};", main, re.S).group(0)
        shader_dir = str(Path(shader_rel).parent).replace("\\", "/")
        for file in ("rzm_drag_collision_surface.hlsl", "rzm_drag_collision_bounds.hlsl"):
            source = (ROOT / "Toolset/drag_interaction" / file).read_text("utf-8")
            source = re.sub(r"struct VertexAttributes\s*\{[^}]*\};", lambda _: vertex_struct, source, flags=re.S)
            shaders[inside(mod, shader.parent / file)] = source
        block = re.sub(r"(?m)^x115\s*=.*\n?", "", block)
        block = re.sub(r"(?m)^run = CustomShaderDragCollision(?:Leaves|Roots|Surface|Apply)?" + re.escape(suffix) + r"\s*\n", "", block)
        block = block.replace("\nDispatch =", "\nx115 = 1\nDispatch =", 1)
        chain = "\n".join(f"run = CustomShaderDragCollision{stage}{suffix}" for stage in ("Leaves", "Roots", "Surface", "Apply"))
        if "\nvb0 = null" not in block:
            raise ValueError("Missing final VB0 publication boundary")
        block = block.replace("\nvb0 = null", "\n" + chain + "\nvb0 = null", 1)
        text = text[:begin] + block + text[end:]
        resource_dir = order_file.relative_to(mod).as_posix()
        extra = baker.surface_shader_sections(suffix, leaves, len(cloth), dispatch, shader_dir)
        extra[f"[ResourceDragCollisionOrder_{suffix}]"] = [
            "type = Buffer", "format = R32_UINT", f"filename = {resource_dir}"]
        extra[f"[ResourceDragCollisionBounds_{suffix}]"] = [
            "type = RWBuffer", "format = R32G32B32A32_FLOAT", f"array = {leaves * 4}"]
        extra[f"[ResourceDragCollisionCloth_{suffix}]"] = [
            "type = Buffer", "format = R32_UINT", f"filename = {cloth_file.relative_to(mod).as_posix()}"]
        extra[f"[ResourceDragCollisionAdjacency_{suffix}]"] = [
            "type = Buffer", "format = R32_UINT", f"filename = {adjacency_file.relative_to(mod).as_posix()}"]
        extra[f"[ResourceDragCollisionFaceLimits_{suffix}]"] = [
            "type = RWBuffer", "format = R32_FLOAT", f"array = {len(cloth)}"]
        for section, body in extra.items():
            text = replace_section(text, section[1:-1], body)
        info.append((suffix, len(triangles), leaves))
    if not info:
        raise ValueError("No triangle-collider jiggle sections found")
    newline = "\r\n" if b"\r\n" in original else "\n"
    payloads[ini] = (b"\xef\xbb\xbf" if bom else b"") + text.replace("\n", newline).encode("utf-8")
    payloads.update({p: text.encode("utf-8") for p, text in shaders.items()})
    return payloads, shaders, info


def compile_shaders(shaders):
    compiler = load("sweep_installer_compiler", ROOT / "tools/compile-hlsl.py")
    compile_fn = compiler.load_compiler()
    compile_fn.restype = ct.c_int32
    output = {}
    for path, source in shaders.items():
        print(f"Compiling {path.name}", flush=True)
        raw = source.encode("utf-8")
        code, error = ct.c_void_p(), ct.c_void_p()
        hr = compile_fn(raw, ct.c_size_t(len(raw)), str(path).encode("utf-8"), None, None,
                        b"main", b"cs_5_0", 0x8000, 0, ct.byref(code), ct.byref(error))
        try:
            if hr < 0:
                raise RuntimeError(compiler.read_blob(error.value).decode("utf-8", "replace"))
            output[path.with_suffix(".cs_5_0.8000.bin")] = compiler.read_blob(code.value)
        finally:
            for blob in (code, error):
                if blob.value:
                    table = ct.cast(blob, ct.POINTER(ct.POINTER(ct.c_void_p))).contents
                    ct.WINFUNCTYPE(ct.c_uint, ct.c_void_p)(table[2])(blob)
    return output


def install(mod, ini, apply=False):
    payloads, shaders, info = prepare(mod, ini)
    changed = {p: data for p, data in payloads.items() if not p.exists() or p.read_bytes() != data}
    for suffix, tris, leaves in info:
        print(f"{suffix}: {tris} triangles, {leaves} padded leaves, {leaves * 64} bytes of live bounds")
    print(f"Changed files: {len(changed)}", flush=True)
    if not apply or not changed:
        return None
    changed_shaders = {p: text for p, text in shaders.items() if p in changed}
    caches = compile_shaders(changed_shaders)
    changed.update(caches)
    # Mods are scanned recursively. A backup containing an .ini MUST be below
    # DISABLED*, otherwise it is loaded as a second, incomplete copy of the mod.
    mods_root = next((parent for parent in mod.parents if parent.name.casefold() == "mods"), None)
    backup_root = (mods_root.parent / "ModBackups" / mod.relative_to(mods_root)) if mods_root else mod / "DISABLED_Backups"
    backup = backup_root / ("drag-collision-sweep-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir(parents=True, exist_ok=False)
    written = []
    existed = set()
    try:
        # Publish the INI last, once every referenced asset is in place.
        for path, data in sorted(changed.items(), key=lambda item: item[0].suffix.casefold() == ".ini"):
            relative = inside(mod, path).relative_to(mod)
            saved = backup / relative
            if path.exists():
                existed.add(path)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
            path.parent.mkdir(parents=True, exist_ok=True)
            written.append(path)
            staged = path.with_name(path.name + ".drag-collision.tmp")
            staged.write_bytes(data)
            os.replace(staged, path)
        for path in caches:
            shader = path.with_name(path.name.split(".cs_5_0.")[0] + ".hlsl")
            stamp = shader.stat()
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        (backup / "new-files.txt").write_text("\n".join(str(p.relative_to(mod)) for p in written if p not in existed), "utf-8")
    except Exception:
        for path in reversed(written):
            saved = backup / path.relative_to(mod)
            if path in existed:
                shutil.copy2(saved, path)
            else:
                path.unlink(missing_ok=True)
        raise
    print(f"Installed. Backup: {backup}", flush=True)
    return backup


def restore(mod, backup):
    """Restore only files recorded by this installer; retain the backup itself."""
    backup = backup.resolve()
    if not (backup / "new-files.txt").is_file():
        raise ValueError("Not a completed collision-install backup")
    saved = [p for p in backup.rglob("*") if p.is_file() and p.name != "new-files.txt"]
    restored = 0
    for source in saved:
        target = inside(mod, mod / source.relative_to(backup))
        if target.is_relative_to(backup):
            raise ValueError("Backup cannot overwrite itself")
        shutil.copy2(source, target)
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
            raise RuntimeError(f"Restore checksum mismatch: {target}")
        restored += 1
    removed = 0
    for relative in (backup / "new-files.txt").read_text("utf-8").splitlines():
        target = inside(mod, mod / relative)
        if target.is_relative_to(backup):
            raise ValueError("Backup cannot remove itself")
        if target.exists():
            target.unlink()
            removed += 1
    print(f"Restored {restored} baseline files; removed {removed} newly installed files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod", type=Path, required=True)
    parser.add_argument("--ini", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore", type=Path, help="Restore an installation backup without compiling")
    args = parser.parse_args()
    if args.restore:
        restore(args.mod.resolve(), args.restore)
        return
    mod = args.mod.resolve()
    candidates = list(mod.glob("*.ini")) if args.ini is None else [inside(mod, mod / args.ini)]
    if len(candidates) != 1:
        parser.error("Specify --ini when the mod has more than one INI file")
    install(mod, candidates[0], args.apply)


if __name__ == "__main__":
    main()
