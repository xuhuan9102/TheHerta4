"""Install Glow FX capture support into EFMITTL 0.5.2 or TTLib 2.41."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import shutil


ASSETS = Path(__file__).resolve().parents[1] / "Toolset" / "glow_fx"
HALO_MARKER = "; Glow FX capture extension with EFMITTL halo resolve."
RADIUS_GROUPS = 8
LAYER_SURFACES = tuple(f"L{layer}{face}" for layer in range(4) for face in ("Front", "Back"))


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError(f"Unexpected EFMITTL configuration near {before.strip()!r}")
    return text.replace(before, after, 1)


def patch_capture(text):
    settings = "\n".join(
        f"global $glowfx_{name} = 0" for name in ("hue", "saturation", "value", "brightness", "only")
    )
    text = replace_once(text, "global $vs_kind = 0\n", f"global $vs_kind = 0\n{settings}\n")
    reset = "ResourceMask = null\n$count = 0\n$first = 0\n"
    if text.count(reset) != 2:
        raise ValueError("Could not locate both EFMITTL call and frame resets")
    text = text.replace(reset, reset + "ResourceGlowFXMap = null\n$glowfx_brightness = 0\n$glowfx_only = 0\n")
    native_draws = "    run = CustomShaderCaptureBack\n    run = CustomShaderCaptureFront\n"
    text = replace_once(text, native_draws, (
        "    local $capture_glow = $glowfx_brightness > 0 && ResourceGlowFXMap !== null\n"
        "    local $capture_glow_only = $capture_glow && $glowfx_only\n"
        "    if !$capture_glow_only\n"
        "        run = CustomShaderCaptureBack\n"
        "        run = CustomShaderCaptureFront\n"
        "    endif\n"
    ))
    alpha_draws = "    run = CustomShaderAlphaBack\n    run = CustomShaderAlphaFront\n"
    text = replace_once(text, alpha_draws, (
        "    if !$capture_glow_only\n"
        "        run = CustomShaderAlphaBack\n"
        "        run = CustomShaderAlphaFront\n"
        "    endif\n"
        "    if $capture_glow\n"
        "        run = CommandListCaptureGlowFX\n"
        "    endif\n"
    ))
    return text


def ini_section(name, lines):
    return "\n".join((f"[{name}]", *lines))


def halo_sections():
    sections = []
    for surface in LAYER_SURFACES:
        sections.append(ini_section(f"ResourceGlowFX{surface}", ["format = R16G16B16A16_FLOAT"]))

    register = ["$glowfx_capture_group = 0"]
    for slot in range(1, RADIUS_GROUPS + 1):
        condition = "if" if slot == 1 else "elif"
        register.extend([f"{condition} $glowfx_group_radius{slot} == $glowfx_radius",
                         f"    $glowfx_capture_group = {slot}"])
    for slot in range(1, RADIUS_GROUPS + 1):
        register.extend([f"elif $glowfx_group_radius{slot} == 0",
                         f"    $glowfx_group_radius{slot} = $glowfx_radius",
                         f"    $glowfx_group_norm{slot} = $glowfx_normalization",
                         f"    $glowfx_capture_group = {slot}",
                         "    $glowfx_halo_pending = 1"])
    sections.append(ini_section("CommandListGlowFXRegisterRadius", [*register, "endif"]))

    select = ["$glowfx_capture_group = 0",
              "if $glowfx_brightness > 0 && $glowfx_radius > 0 && ResourceGlowFXMap !== null",
              "    run = CommandListGlowFXRegisterRadius", "endif"]
    for layer in range(4):
        select.append(f"{'if' if layer == 0 else 'elif'} $layer == {layer}")
        for face in ("Front", "Back"):
            resource = f"ResourceGlowFXL{layer}{face}"
            select.extend([f"    if $glowfx_capture_group && {resource} === null",
                           f"        {resource} = copy_desc o0",
                           f"        clear = {resource}", "    endif",
                           f"    ResourceGlowFX{face} = ref {resource}"])
    sections.append(ini_section("CommandListGlowFXSelectEmitters", [*select, "endif"]))

    clear = []
    for face, cull in (("Back", "front"), ("Front", "back")):
        targets = f"CommandListGlowFX{face}EmitterTargets"
        sections.append(ini_section(targets, [
            "run = BuiltInCommandListUnbindAllRenderTargets", f"o0 = ref ResourceGlowFX{face}",
            f"oD = ref Resource{face}Depth", "drawindexed = $count, $first, 0",
        ]))
        clear.extend([f"if ResourceGlowFX{face} !== null", f"    run = CustomShaderGlowFXClear{face}", "endif"])
        for kind, shader in (("Emitter", "capture"), ("Clear", "halo_clear")):
            sections.append(ini_section(f"CustomShaderGlowFX{kind}{face}", [
                f"ps = shaders\\glow_fx_{shader}.hlsl", f"cull = {cull}", "front = CounterClockwise",
                "depth_clip_enable = true", "depth_enable = true", "depth_write_mask = zero",
                "depth_func = equal", "stencil_enable = false", "blend = disable", f"run = {targets}",
            ]))
    sections.append(ini_section("CommandListGlowFXClearReplacedEmitters", clear))

    bind = []
    restore = []
    for index, surface in enumerate(LAYER_SURFACES):
        slot = 90 + index
        saved = f"ResourceGlowFXSavedT{slot}"
        sections.append(ini_section(saved, []))
        bind.extend([f"{saved} = ref ps-t{slot}", f"ps-t{slot} = ref ResourceGlowFX{surface}"])
        restore.extend([f"ps-t{slot} = ref {saved}", f"{saved} = null"])
    sections.append(ini_section("CommandListGlowFXBindEmitters", bind))
    sections.append(ini_section("CommandListGlowFXRestoreEmitters", restore))
    depth_layers = [f"ps-t{100 + i} = ref Resource{surface}Color" for i, surface in enumerate(LAYER_SURFACES)]
    depth_layers.extend(f"ps-t{108 + i} = ref Resource{surface}Depth" for i, surface in enumerate(LAYER_SURFACES))
    sections.append(ini_section("CommandListGlowFXBindDepthLayers", [*depth_layers, "ps-t116 = ref ResourceSceneDepth"]))
    sections.append(ini_section("CommandListGlowFXUnbindDepthLayers", [f"ps-t{i} = null" for i in range(100, 117)]))

    compose = []
    reset = ["$glowfx_halo_pending = 0", "ResourceGlowFXFront = null", "ResourceGlowFXBack = null"]
    reset.extend(f"ResourceGlowFX{surface} = null" for surface in LAYER_SURFACES)
    for slot in range(1, RADIUS_GROUPS + 1):
        radius = f"$glowfx_group_radius{slot}"
        compose.extend([f"if {radius} > 0", f"    x181 = {slot}", f"    y181 = {radius}",
                        f"    z181 = $glowfx_group_norm{slot}", "    run = CommandListGlowFXComposeGroup", "endif"])
        reset.append(f"{radius} = 0")
    sections.append(ini_section("CommandListGlowFXComposeGroups", compose))
    sections.append(ini_section("CommandListGlowFXResetHalo", reset))
    return "\n\n".join(sections) + "\n"


def patch_config(text):
    if "global locked $version = 0.52\n" not in text:
        raise ValueError("This extension requires EFMITTL 0.5.2")
    # Both forward variants pass position and the primary UV in o0 and o1.
    # Use the same declaration pattern as the standalone replay shader.
    standalone = (ASSETS / "EFMI/GlowFX.ini").read_text(encoding="utf-8")
    _, _, replay_pattern = standalone.partition("[ShaderRegexEFMIGlowFXLitVS.Pattern]\n")
    forward_pattern = replay_pattern.split("\n", 1)[0]
    header = "[ShaderRegexEFMITTLLitVS.Pattern]\n"
    _, _, capture_pattern = text.partition(header)
    current_pattern = capture_pattern.split("\n", 1)[0]
    if not current_pattern or not forward_pattern:
        raise ValueError("Could not locate the EFMI forward VS declaration pattern")
    if current_pattern != forward_pattern:
        legacy_pattern = forward_pattern.split(r"(?:dcl_output o8", 1)[0] + r"dcl_output o8\.xyz\ndcl_output o9\.x\n"
        if current_pattern != legacy_pattern:
            raise ValueError("Unexpected EFMITTL forward VS declaration pattern")
        text = replace_once(text, header + legacy_pattern + "\n", header + forward_pattern + "\n")
    if HALO_MARKER in text:
        return text

    capture = (ASSETS / "EFMITTL/glow_fx_sections.ini").read_text(encoding="utf-8")
    if "[CommandListCaptureGlowFX]" in text:
        # Replace the previously installed core-only extension, retaining other sections.
        names = re.findall(r"(?m)^\[([^\]]+)\]$", capture)
        for name in names:
            section = re.compile(rf"(?ms)^\[{re.escape(name)}\]\n.*?(?=^\[|\Z)")
            text, removed = section.subn("", text)
            if removed != 1:
                raise ValueError(f"Expected one old Glow FX section named {name}")
    else:
        text = patch_capture(text)

    globals_ = ["glowfx_radius", "glowfx_normalization", "glowfx_capture_group", "glowfx_halo_pending"]
    for slot in range(1, RADIUS_GROUPS + 1):
        globals_.extend((f"glowfx_group_radius{slot}", f"glowfx_group_norm{slot}"))
    anchor = "global $glowfx_only = 0\n"
    text = replace_once(text, anchor, anchor + "".join(f"global ${name} = 0\n" for name in globals_))
    reset = "$glowfx_only = 0\n"
    text, changed = re.subn(r"(?m)^\$glowfx_only = 0\n", reset + "$glowfx_radius = 0\n$glowfx_normalization = 1\n", text)
    if changed != 2:
        raise ValueError("Could not locate both Glow FX call and frame resets")
    text = replace_once(text, "[CommandListFrameReset]\n", (
        "[CommandListFrameReset]\nif $glowfx_halo_pending\n    run = CommandListGlowFXResetHalo\nendif\n"
    ))
    text = replace_once(text, "[CommandListReleaseLayers]\n", (
        "[CommandListReleaseLayers]\nrun = CommandListGlowFXResetHalo\n"
        "ResourceGlowFXSeedNear = null\nResourceGlowFXSeedFar = null\n"
        "ResourceGlowFXBlurNear = null\nResourceGlowFXBlurFar = null\n"
    ))
    text = replace_once(text, "    if !$capture_glow_only\n        run = CustomShaderAlphaBack\n", (
        "    if $glowfx_halo_pending || ($capture_glow && $glowfx_radius > 0 && $width >= 2 && $height >= 2)\n"
        "        run = CommandListGlowFXSelectEmitters\n"
        "    endif\n"
        "    if !$capture_glow_only\n        run = CustomShaderAlphaBack\n"
    ))
    glow = "    if $capture_glow\n        run = CommandListCaptureGlowFX\n    endif\n"
    text = replace_once(text, glow, (
        "    if $capture_glow\n        run = CommandListCaptureGlowFX\n"
        "    elif ResourceGlowFXFront !== null || ResourceGlowFXBack !== null\n"
        "        run = CommandListGlowFXClearReplacedEmitters\n    endif\n"
    ))
    text = replace_once(text, "    run = CustomShaderComposite\n", (
        "    run = CustomShaderComposite\n"
        "    if $glowfx_halo_pending\n"
        "        run = CommandListApplyGlowFXHalo\n"
        "        run = CommandListGlowFXResetHalo\n"
        "    endif\n"
    ))
    halo = (ASSETS / "EFMITTL/glow_fx_halo.ini").read_text(encoding="utf-8")
    return "\n\n".join((text.rstrip(), capture.rstrip(), halo.rstrip(), halo_sections().rstrip())) + "\n"


def edit_ini(path, edit):
    raw = path.read_bytes()
    result = edit(raw.decode("utf-8-sig").replace("\r\n", "\n"))
    if b"\r\n" in raw:
        result = result.replace("\n", "\r\n")
    bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
    return bom + result.encode("utf-8")


def efmittl_outputs(library, ini):
    return {
        **{library / "shaders" / shader.name: shader.read_bytes()
           for shader in (ASSETS / "EFMITTL").glob("*.hlsl*")},
        **{library / "shaders" / name: (ASSETS / "res" / name).read_bytes()
           for name in ("glow_fx_emission.hlsli", "geom_glow_layers.hlsli")},
        ini: edit_ini(ini, patch_config),
    }


def patch_ttlib(text):
    if "global locked $version = 2.41\n" not in text:
        raise ValueError("This extension requires TTLib 2.41")
    if "run = CommandListCaptureGlowFX" in text:
        return text
    start = text.index("[CommandListCaptureLegacyDraw]")
    end = text.index("[CommandListCaptureObjectDraw]", start)
    capture = replace_once(text[start:end], "    $oit_defer_inner_restore = 1\n", (
        "    if @ResourceGlowFXMap && $glowfx_brightness > 0\n"
        "        run = CommandListCaptureGlowFX\n"
        "    endif\n"
        "    $oit_defer_inner_restore = 1\n"
    ))
    text = text[:start] + capture + text[end:]
    for name in ("CommandListFrameReset", "CommandListResetCallParams"):
        header = f"[{name}]\n"
        text = replace_once(text, header, header + "ResourceGlowFXMap = null\n$glowfx_brightness = 0\n")
    return text


def ttlib_outputs(library, ini):
    def include(text):
        if "include = GlowFXCapture.ini" in text:
            return text
        return text.rstrip("\n") + "\n\n[Include TTLibGlowFX]\ninclude = GlowFXCapture.ini\n"

    includes = library / "Includes.ini"
    return {
        library / "GlowFXCapture.ini": (ASSETS / "TTLib/GlowFXCapture.ini").read_bytes(),
        library / "Shaders/CaptureGlowFX.hlsl": (ASSETS / "TTLib/Shaders/CaptureGlowFX.hlsl").read_bytes(),
        library / "Shaders/glow_fx_emission.hlsli": (ASSETS / "res/glow_fx_emission.hlsli").read_bytes(),
        includes: edit_ini(includes, include),
        ini: edit_ini(ini, patch_ttlib),
    }


def install(library, backup_root=None):
    efmi = library / "translucency.ini"
    if efmi.is_file():
        outputs = efmittl_outputs(library, efmi)
        game_root = library.parent.parent
    else:
        outputs = ttlib_outputs(library, library / "TTLib.ini")
        game_root = next((parent for parent in library.parents if (parent / "d3dx.ini").is_file()), None)
        if game_root is None and backup_root is None:
            raise ValueError("Use --backups to specify a directory outside the loader's include tree")

    changed = {p: data for p, data in outputs.items() if not p.exists() or p.read_bytes() != data}
    if not changed:
        return None
    backup_root = backup_root or game_root / "Backups"
    label = "EFMITTL" if efmi.is_file() else "TTLib"
    backup = backup_root / (f"{label}-glowfx-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    backup.mkdir(parents=True)
    for target in changed:
        if target.exists():
            saved = backup / target.relative_to(library)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)

    installed = []
    try:
        for target, data in changed.items():
            temporary = target.with_suffix(target.suffix + ".glowfx-new")
            temporary.write_bytes(data)
            os.replace(temporary, target)
            installed.append(target)
    except BaseException:
        for target in reversed(installed):
            saved = backup / target.relative_to(library)
            if saved.exists():
                shutil.copy2(saved, target)
            else:
                target.rename(target.with_suffix(target.suffix + ".failed-install"))
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path, help="Directory containing translucency.ini or TTLib.ini")
    parser.add_argument("--backups", type=Path, help="Save originals outside the game's Mods folder")
    args = parser.parse_args()
    backup = install(args.library.resolve(), args.backups)
    print(f"Installed. Originals: {backup}" if backup else "Already installed.")


if __name__ == "__main__":
    main()
