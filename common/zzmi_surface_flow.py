"""Scoped surface sweep for the ZZMI material shaders."""

import re

_MAGIC = 734125
_TEXTURE_MAGIC = 734126


def surface_draw_scope(tag, draw_id, speed, brightness, color, height_scale=0.0, texture=None):
    prefix = f"$zzmi_surface_{tag}_{draw_id}"
    color_slots = tuple(zip(("x496", "y496", "z496"), color))
    saved = f"ResourceZZMISurfaceFlowSaved{tag}"
    return (
        (
            f"local {prefix}_x = x498",
            f"local {prefix}_y = y498",
            f"local {prefix}_phase = x497",
            f"local {prefix}_brightness = y497",
            *(f"local {prefix}_{slot} = {slot}" for slot, _ in color_slots),
            *((f"pre {saved} = ref ps-t119", f"pre ps-t119 = ref {texture}") if texture else ()),
            f"pre x498 = {_TEXTURE_MAGIC if texture else _MAGIC}",
            f"pre y498 = {height_scale:.9f}",
            f"pre x497 = time * {-speed:.6f}",
            f"pre y497 = {brightness:.6f}",
            *(f"pre {slot} = {component:.6f}" for slot, component in color_slots),
        ),
        (
            *((f"pre ps-t119 = ref {saved}", f"pre {saved} = null") if texture else ()),
            f"pre x498 = {prefix}_x",
            f"pre y498 = {prefix}_y",
            f"pre x497 = {prefix}_phase",
            f"pre y497 = {prefix}_brightness",
            *(f"pre {slot} = {prefix}_{slot}" for slot, _ in color_slots),
        ),
    )


_FLOW_PATTERN = (
    # Two periodic waves produce a tapered crest with a curved edge.
    "mul ${flowtex}.w, ${flowa}.x, l(6.2831853)",
    "sincos ${flowtex}.z, null, ${flowtex}.w",
    "mad ${flowtex}.w, ${flowtex}.z, l(0.14), ${flowa}.y",
    "frc ${flowtex}.w, ${flowtex}.w",
    "mul ${flowtex}.w, ${flowtex}.w, l(6.2831853)",
    "sincos null, ${flowtex}.x, ${flowtex}.w",
    "mad_sat ${flowtex}.x, ${flowtex}.x, l(0.5), l(0.5)",
    "mul ${flowtex}.x, ${flowtex}.x, ${flowtex}.x",
)


def _camera_direction(camera, position):
    return (
        "mov ${flowh}.x, v2.w",
        "mov ${flowh}.y, v3.w",
        "mov ${flowh}.z, v4.w",
        f"add ${{flowh}}.xyz, cb0[{camera}].xyzx, -${{flowh}}.xyzx",
    ) if position is None else (
        f"add ${{flowh}}.xyz, cb0[{camera}].xyzx, -{position}.xyzx",
    )


def _mirror_highlight(normal, camera, position=None, textured=False):
    return (
        "ld_indexable(texture1d)(float,float,float,float) ${flowd}.xy, l(497, 0, 0, 0), t120.xyzw",
        *_camera_direction(camera, position),
        "dp3 ${flowh}.w, ${flowh}.xyzx, ${flowh}.xyzx",
        "max ${flowh}.w, ${flowh}.w, l(0.000001)",
        "rsq ${flowh}.w, ${flowh}.w",
        "mul ${flowh}.xyz, ${flowh}.xyzx, ${flowh}.wwww",
        f"dp3 ${{flowa}}.x, {normal}, {normal}",
        "max ${flowa}.x, ${flowa}.x, l(0.000001)",
        "rsq ${flowa}.x, ${flowa}.x",
        f"mul ${{flowtex}}.xyz, {normal}, ${{flowa}}.xxxx",
        "dp3 ${flowa}.z, ${flowtex}.xyzx, ${flowh}.xyzx",
        "add ${flowa}.w, ${flowa}.z, ${flowa}.z",
        # Drive the procedural waves from a continuous reflection direction.
        "mad ${flowh}.xyz, ${flowtex}.xyzx, ${flowa}.wwww, -${flowh}.xyzx",
        "mad ${flowa}.x, ${flowh}.x, l(0.5), l(0.5)",
        "dp2 ${flowa}.y, ${flowh}.yzyy, l(0.45, 0.20, 0.0, 0.0)",
        "add ${flowa}.y, ${flowa}.y, l(0.5)",
        "add ${flowa}.y, ${flowa}.y, ${flowd}.x",
        "frc ${flowa}.y, ${flowa}.y",
        *(("mov ${flowd}.zw, ${flowa}.xxxy",) if textured else ()),
        *_FLOW_PATTERN,
        "mov ${flowa}.z, l(0.0)",
        "mul ${flowa}.x, ${flowd}.y, l(0.09)",
    )


def _head_highlight_normal(normal, position, head_buffer):
    # Match the native head-region normal blend around the character's sphere.
    world_position = (
        "mov ${flown}.x, v2.w",
        "mov ${flown}.y, v3.w",
        "mov ${flown}.z, v4.w",
    ) if position is None else (
        f"mov ${{flown}}.xyz, {position}.xyzx",
    )
    if head_buffer == "cb3":
        # Recover the head center from the rows of its inverse rigid transform.
        # Row 5 points down the head axis; the center is 8 cm above its origin.
        center_delta = (
            "add ${flowa}.x, cb3[5].w, l(0.08)",
            "mad ${flown}.xyz, cb3[5].xyzx, ${flowa}.xxxx, ${flown}.xyzx",
            "mad ${flown}.xyz, cb3[6].xyzx, cb3[6].wwww, ${flown}.xyzx",
            "mad ${flown}.xyz, cb3[7].xyzx, cb3[7].wwww, ${flown}.xyzx",
        )
        radius = "l(0.13)"
    else:
        center_delta = ("add ${flown}.xyz, ${flown}.xyzx, -cb2[4].xyzx",)
        radius = "cb2[4].w"
    return (
        *world_position,
        *center_delta,
        "dp3 ${flowa}.x, ${flown}.xyzx, ${flown}.xyzx",
        "max ${flowa}.x, ${flowa}.x, l(0.000001)",
        "rsq ${flowa}.w, ${flowa}.x",
        "sqrt ${flowa}.x, ${flowa}.x",
        f"add ${{flowa}}.x, ${{flowa}}.x, -{radius}",
        "mad_sat ${flowa}.x, -${flowa}.x, l(20.0), l(1.0)",
        *((
            "lt ${flown}.w, l(0.0), cb2[4].w",
            "and ${flowa}.x, ${flowa}.x, ${flown}.w",
        ) if head_buffer == "cb2" else ()),
        f"mad ${{flown}}.xyz, ${{flown}}.xyzx, ${{flowa}}.wwww, -{normal}",
        f"mad ${{flown}}.xyz, ${{flowa}}.xxxx, ${{flown}}.xyzx, {normal}",
    )


def _height_highlight(normal, camera, position=None, textured=False):
    return (
        "ld_indexable(texture1d)(float,float,float,float) ${flowd}.xy, l(497, 0, 0, 0), t120.xyzw",
        "add ${flowa}.y, ${flowa}.y, ${flowd}.x",
        "frc ${flowa}.y, ${flowa}.y",
        "mov ${flowa}.x, l(0.5)",
        *((
            f"mad_sat ${{flowd}}.z, {normal.split('.')[0]}.xxxx, l(0.5), l(0.5)",
            "mov ${flowd}.w, ${flowa}.y",
        ) if textured else ()),
        *_FLOW_PATTERN,
        *_camera_direction(camera, position),
        "dp3 ${flowh}.w, ${flowh}.xyzx, ${flowh}.xyzx",
        "max ${flowh}.w, ${flowh}.w, l(0.000001)",
        "rsq ${flowh}.w, ${flowh}.w",
        "mul ${flowh}.xyz, ${flowh}.xyzx, ${flowh}.wwww",
        f"dp3 ${{flowa}}.x, {normal}, ${{flowh}}.xyzx",
        "add_sat ${flowa}.x, -|${flowa}.x|, l(1.0)",
        "mul ${flowa}.z, ${flowa}.x, ${flowa}.x",
        "mul ${flowa}.z, ${flowa}.z, ${flowa}.z",
        "mul ${flowa}.x, ${flowa}.x, ${flowa}.z",
        "sqrt ${flowa}.z, ${flowa}.x",
        "mul ${flowa}.x, ${flowa}.x, ${flowd}.y",
        "mul ${flowa}.x, ${flowa}.x, l(0.5)",
    )


def _relative_height(coord, world_y, origin_y):
    return (
        f"ld_indexable(texture1d)(float,float,float,float) {coord}.z, l(498, 0, 0, 0), t120.yyyy",
        f"add {coord}.y, {world_y}, -{origin_y}",
        f"mad_sat {coord}.y, {coord}.yyyy, {coord}.zzzz, l(0.5)",
    )


def _capture_registers(pattern, normal, accumulator):
    """Match the same data flow after the compiler renumbers temporaries."""
    registers = {}

    def capture(match):
        register = match[0]
        if register in registers:
            return rf"\k<{registers[register]}>"
        name = f"temp_{register}"
        registers[register] = name
        return rf"(?<{name}>r[0-9]+)"

    pattern = re.sub(r"\br[0-9]+\b", capture, pattern)

    def operand(value):
        register, separator, swizzle = value.partition(".")
        if re.fullmatch(r"r[0-9]+", register):
            return "${" + registers[register] + "}" + separator + swizzle
        return value

    return pattern, operand(normal), operand(accumulator)


def surface_shader_sections(textured=False):
    shader = "ShaderRegexTheHertaZZMISurfaceFlow"
    sections = {}
    face_color = r"\.xyz, r4\.xxxx, r2\.xyzx, r0\.xyzx\r?\n"
    face_tail = (
        r"(?=[ \t]*movc r0\.x, r9\.x, l\(0\.340000\), l\(0\)\r?\n"
        r"[ \t]*movc o2\.w,[^\r\n]*\r?\n[ \t]*mad o3\.xyz, r3\.xyzx, l\(0\.500000,)"
    )
    branch_color = (
        "${color_start}${flowcolor}${color_middle}${flowcolor}"
        "${color_end}${flowcolor}${color_tail}"
    )
    face_outputs = {
        "NativeFaceColor": ("v4", branch_color),
        "NativeFaceColorDirect": ("v4", "${color_start}${flowcolor}${color_tail}"),
        "NativeFaceClose": ("v4", "${color_start}${flowcolor}${color_tail}"),
        "NativeFaceCloseBranched": ("v4", branch_color),
        "NativeFaceDeferred": ("v4", "${color_start}${flowcolor}${color_tail}"),
        "NativeFaceDeferredBranched": ("v4", branch_color),
        "NativeFaceOverlay": ("v3", None),
    }
    # These shader layouts bind the shared character origin in CB2[0].
    # CB1[3] instead follows the individual draw (the head moves independently).
    shared_origin = {
        "", "AgentSceneColor", "NativeFaceDeferred",
        "NativeFaceDeferredBranched", "NativeFaceOverlay",
    }
    deferred_origin = {
        "DeferredColor", "NativeFaceColor", "NativeFaceColorDirect",
        "NativeFaceClose", "NativeFaceCloseBranched",
    }
    for suffix, opening, ending, normal, camera, accumulator, components, after in (
        (
            "",
            r"lt r0\.x, l\(0\.500000\), cb3\[125\]\.x\r?\n",
            r".*?^[ \t]*mad r15\.xyz, cb3\[95\]\.wwww, r0\.xyzx, r15\.xyzx\r?\n[ \t]*endif[^\r\n]*\r?\n",
            "v2.xyzx", 55, "r15", "xyz", True,
        ),
        (
            "Deferred",
            r"lt r0\.w, l\(0\.500000\), cb4\[161\]\.z\r?\n",
            r".*?mad r0\.xyz, cb4\[121\]\.wwww, r2\.xyzx, r0\.xyzx\r?\n[ \t]*mov r5\.w, l\(0\)\r?\n[ \t]*endif[^\r\n]*\r?\n",
            "v2.xyzx", 53, "r0", "xyz", True,
        ),
        (
            "DeferredColor",
            r"mul r1\.w, cb4\[29\]\.w, cb4\[158\]\.w\r?\n",
            r"(?=[ \t]*add r2\.xyz, -r1\.xyzx, cb4\[29\]\.xyzx\r?\n.*?mad o3\.xyz, r3\.xywx, l\(0\.500000,)",
            "r3.xywx", 53, "r1", "xyz", False,
        ),
        (
            "AgentSceneColor",
            r"mul r0\.xyw, r0\.xyxw, r3\.xyxz\r?\n",
            r"(?=(?:(?!^[ \t]*ret\b).)*?^[ \t]*add (?:o0|r[0-9]+)\.xyz, r2\.xzwx, r1\.xywx\r?\n"
            r"(?:(?!^[ \t]*ret\b).)*?mad o3\.xyz, r4\.xyzx, l\(0\.500000,)",
            "r4.xyzx", 55, "r2", "xzw", False,
        ),
        (
            "NativeFaceColor",
            r"(?<color_start>mul r1\.x, cb4\[25\]\.w, cb4\[122\]\.w\r?\n"
            r".*?\bmov )o0(?<color_middle>\.xyz, r31\.xyz\r?\n"
            r".*?\bmax )o0(?<color_end>\.xyz, r30\.xyz, r31\.xyz\r?\n"
            r".*?\bmad )o0",
            r"(?<color_tail>\.xyz, r1\.xxxx, r1\.yzwy, r0\.xyzx\r?\n[ \t]*endif[^\r\n]*\r?\n)"
            r"(?=.*?mad o3\.xyz, r3\.xyzx, l\(0\.500000,)",
            "r3.xyzx", 53, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceColorDirect",
            r"(?<color_start>mul r1\.x, cb4\[25\]\.w, cb4\[122\]\.w\r?\n"
            r"[ \t]*add r1\.yzw, -r0\.xxyz, cb4\[25\]\.xxyz\r?\n[ \t]*mad )o0"
            r"(?<color_tail>\.xyz, r1\.xxxx, r1\.yzwy, r0\.xyzx\r?\n)",
            r"(?=[ \t]*mad r0\.x, -cb4\[25\]\.w, cb4\[122\]\.w, l\(1\.000000\)"
            r"(?:(?!^[ \t]*ret\b).)*?mad o3\.xyz, r3\.xyzx, l\(0\.500000,)",
            "r3.xyzx", 53, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceDeferred",
            r"(?<color_start>movc )o0(?<color_tail>" + face_color + ")",
            face_tail,
            "r3.xyzx", 55, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceClose",
            r"(?<color_start>mad o3\.xyz, r3\.xyzx, l\(0\.500000,[^\r\n]*\r?\n"
            r"[ \t]*mov )o0(?<color_tail>\.xyz, r2\.xyzx\r?\n)",
            r"(?=[ \t]*mov o0\.w, l\(1\.000000\))",
            "r3.xyzx", 53, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceCloseBranched",
            r"(?<color_start>mad o3\.xyz, r3\.xyzx, l\(0\.500000,[^\r\n]*\r?\n"
            r"[ \t]*if_nz r27\.x\r?\n[ \t]*if_z r25\.z\r?\n[ \t]*mov )"
            r"o0(?<color_middle>\.xyz, r32\.xyz\r?\n[ \t]*else[^\r\n]*\r?\n"
            r"[ \t]*mov r31\.xyz, r2\.xyzx\r?\n[ \t]*max )"
            r"o0(?<color_end>\.xyz, r31\.xyz, r32\.xyz\r?\n[ \t]*endif[^\r\n]*\r?\n"
            r"[ \t]*else[^\r\n]*\r?\n[ \t]*mov )"
            r"o0(?<color_tail>\.xyz, r2\.xyzx\r?\n[ \t]*endif[^\r\n]*\r?\n)",
            r"(?=[ \t]*mov o0\.w, l\(1\.000000\))",
            "r3.xyzx", 53, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceDeferredBranched",
            r"(?<color_start>if_nz r26\.x\r?\n[ \t]*if_z r24\.z\r?\n[ \t]*mov )"
            r"o0(?<color_middle>\.xyz, r31\.xyz\r?\n[ \t]*else[^\r\n]*\r?\n"
            r"[ \t]*movc r30" + face_color + r"[ \t]*max )"
            r"o0(?<color_end>\.xyz, r30\.xyz, r31\.xyz\r?\n[ \t]*endif[^\r\n]*\r?\n"
            r"[ \t]*else[^\r\n]*\r?\n[ \t]*movc )"
            r"o0(?<color_tail>" + face_color + r"[ \t]*endif[^\r\n]*\r?\n)",
            face_tail,
            "r3.xyzx", 55, "${flowcolor}", "xyz", True,
        ),
        (
            "NativeFaceOverlay",
            r"mad r0\.xyz, r1\.xyzx, r2\.xyzx, r0\.xyzx\r?\n",
            r"(?=[ \t]*add_sat r1\.xyz, r0\.xyzx, -cb0\[20\]\.zzzz\r?\n"
            r".*?mov o3\.xyzw, l\(0\.500000,\s*0\.500000,\s*0\.500000,\s*1\.000000\))",
            "v5.xyzx", 55, "r0", "xyz", True,
        ),
    ):
        name = shader + suffix
        pattern, normal, accumulator = _capture_registers(opening + ending, normal, accumulator)
        position, color_output = face_outputs.get(suffix, (None, None))
        head_buffer = "cb2" if suffix in shared_origin else "cb3" if suffix in deferred_origin else None
        origin_y = f"{head_buffer}[0].y" if head_buffer else "cb1[3].y"
        flow_normal = "${flown}.xyzx" if head_buffer else normal
        base = f"{accumulator}." + ("xyzx" if components == "xyz" else "xxzw")
        tint = "${flowd}." + ("xyzx" if components == "xyz" else "xxyz")
        patch = (
            "ld_indexable(texture1d)(float,float,float,float) ${flowa}.x, l(498, 0, 0, 0), t120.xxxx",
            *((f"eq ${{flowa}}.w, ${{flowa}}.x, l({_TEXTURE_MAGIC}.0)",) if textured else ()),
            f"eq ${{flowa}}.x, ${{flowa}}.x, l({_MAGIC}.0)",
            *(("or ${flowa}.x, ${flowa}.x, ${flowa}.w",) if textured else ()),
            "if_nz ${flowa}.x",
            *(_head_highlight_normal(normal, position, head_buffer) if head_buffer else ()),
            "ld_indexable(texture1d)(float,float,float,float) ${flowa}.z, l(498, 0, 0, 0), t120.yyyy",
            "if_nz ${flowa}.z",
            *_relative_height("${flowa}", f"{position}.y" if position else "v3.w", origin_y),
            *_height_highlight(flow_normal, camera, position, textured),
            "else",
            *_mirror_highlight(flow_normal, camera, position, textured),
            "endif",
            "mad ${flowa}.z, ${flowa}.z, l(0.5), l(0.5)",
            *((
                "ld_indexable(texture1d)(float,float,float,float) ${flowa}.w, l(498, 0, 0, 0), t120.xxxx",
                f"eq ${{flowa}}.w, ${{flowa}}.w, l({_TEXTURE_MAGIC}.0)",
                "if_nz ${flowa}.w",
                "sample_l_indexable(texture2d)(float,float,float,float) ${flowtex}.xyzw, ${flowd}.zwzz, t119.xyzw, s0, l(0.0)",
                "mov ${flowd}.xyz, ${flowtex}.xyzx",
                "mul ${flowa}.z, ${flowa}.z, ${flowtex}.w",
                "else",
            ) if textured else ()),
            "ld_indexable(texture1d)(float,float,float,float) ${flowd}.xyz, l(496, 0, 0, 0), t120.xyzw",
            "mul ${flowa}.z, ${flowa}.z, ${flowtex}.x",
            *(("endif",) if textured else ()),
            f"mul ${{flowh}}.{components}, {tint}, ${{flowa}}.zzzz",
            f"mul ${{flowh}}.{components}, ${{flowh}}.xyzw, ${{flowa}}.xxxx",
            # Keep the native result and limit only the additional highlight.
            f"add_sat ${{flowd}}.{components}, -{base}, l(1.0)",
            f"min ${{flowh}}.{components}, ${{flowh}}.xyzw, ${{flowd}}.xyzw",
            f"add {accumulator}.{components}, ${{flowh}}.xyzw, {base}",
            "endif",
        )
        assembly = [line + r"\n" for line in patch]
        options = ["shader_model = ps_5_0", "temps = flowa flowd flowh flowtex"]
        if head_buffer:
            options[1] += " flown"
        if suffix == "AgentSceneColor":
            options.append("local $zzmi_surface_pattern_matched = 1")
        anchor = "${anchor}"
        if color_output:
            options[1] += " flowcolor"
            anchor = color_output
            assembly.append(r"mov o0.xyz, ${flowcolor}.xyzx\n")
        layout = ""
        if position == "v4":
            # Face variants supply position in v4.xyz. Body variants pack it
            # across three .w components and can otherwise share the same tail.
            layout = r"\A(?=.*?^[ \t]*dcl_input_ps linear v4\.xyz[ \t]*\r?$)(?<shader_prefix>.*?)"
        replacement = [anchor, *assembly] if after else [*assembly, anchor]
        if layout:
            replacement.insert(0, "${shader_prefix}")
        sections.update({
            f"[{name}]": options,
            f"[{name}.Pattern]": [r"(?ms)" + layout + r"(?<anchor>^[ \t]*" + pattern + ")"],
            f"[{name}.Pattern.Replace]": replacement,
            f"[{name}.InsertDeclarations]": [
                "dcl_resource_texture1d (float,float,float,float) t120",
                *(("dcl_resource_texture2d (float,float,float,float) t119",) if textured else ()),
            ],
        })
    return sections


