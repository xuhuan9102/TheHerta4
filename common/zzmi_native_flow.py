"""Shared front projection and the ZZMI native height-based material sweep."""

import numpy


UV_LAYER = "TEXCOORD2.xy"

# 只有蓝图里存在 Glow FX 节点且勾选「独立表面流光」时，才允许导出器
# 生成 NativeHeightFlow / PrimaryMaterialUV 这两段 ShaderRegex。
NATIVE_FLOW_NODE = "SSMTNode_PostProcess_GlowFX"


def has_native_flow_node(blueprint_model):
    """Return True only when the blueprint explicitly enables the native flow."""
    return any(
        getattr(node, "bl_idname", "") == NATIVE_FLOW_NODE
        and bool(getattr(node, "enable_native_surface_flow", False))
        for node in getattr(blueprint_model, "postprocess_nodes", ())
    )

_FLOAT_FORMATS = {
    "R16G16_FLOAT": "<f2",
    "R16G16B16A16_FLOAT": "<f2",
    "R32G32_FLOAT": "<f4",
    "R32G32B32_FLOAT": "<f4",
    "R32G32B32A32_FLOAT": "<f4",
}


def _element_view(buffers, game_type, name, component_count):
    offsets = {}
    for element in getattr(game_type, "D3D11ElementList", ()):
        category = element.Category
        offset = offsets.get(category, 0)
        offsets[category] = offset + element.ByteWidth
        if element.ElementName != name:
            continue
        fmt = _FLOAT_FORMATS.get(element.Format)
        data = buffers.get(category)
        if fmt is None or data is None or not len(data):
            return None
        dtype = numpy.dtype(fmt)
        if element.ByteWidth < component_count * dtype.itemsize:
            return None
        stride = game_type.CategoryStrideDict[category]
        count, remainder = divmod(data.nbytes, stride)
        if remainder:
            raise ValueError(f"ZZMI {category} buffer does not match its stride {stride}")
        return numpy.ndarray(
            (count, component_count), dtype=dtype, buffer=data,
            offset=offset, strides=(stride, dtype.itemsize),
        )
    return None


def prepare_native_flow(drawib_models, reproject_uv2):
    """Use the exported geometry's shared Blender-space bounds for UV2 and sweep height."""
    streams = []
    minimums = []
    maximums = []
    for model in drawib_models:
        for submesh in getattr(model, "submesh_model_list", ()):
            buffers = getattr(submesh, "category_buffer_dict", {})
            game_type = getattr(submesh, "d3d11_game_type", None)
            positions = _element_view(buffers, game_type, "POSITION", 3)
            if positions is None:
                continue
            streams.append((buffers, game_type, positions))
            draws = getattr(submesh, "drawcall_model_list", ())
            if not draws or not all(getattr(draw, "zzmi_stub", False) for draw in draws):
                minimums.append(positions.min(axis=0))
                maximums.append(positions.max(axis=0))
        if reproject_uv2:
            buffers = getattr(model, "category_buffer_dict", {})
            game_type = getattr(model, "d3d11GameType", None)
            positions = _element_view(buffers, game_type, "POSITION", 3)
            if positions is not None:
                streams.append((buffers, game_type, positions))

    if not minimums:
        return None
    minimum = numpy.min(minimums, axis=0).astype(numpy.float64)
    maximum = numpy.max(maximums, axis=0).astype(numpy.float64)
    span = maximum - minimum
    if reproject_uv2:
        center = ((minimum + maximum) * 0.5)[[0, 2]]
        size = max(span[0], span[2], 1e-6)
        projected_count = 0
        for buffers, game_type, positions in streams:
            uv = _element_view(buffers, game_type, "TEXCOORD2", 2)
            if uv is None:
                continue
            # Blender Front is X/Z. The exported Direct3D V coordinate points down.
            projected = (positions[:, (0, 2)] - center) / size
            projected[:, 1] *= -1.0
            uv[:] = numpy.clip(projected + 0.5, 0.0, 1.0)
            projected_count += 1
        if projected_count:
            print("[ZZMI原生流光] 已为本次导出网格统一正交前视 TEXCOORD2.xy")

    if span[2] <= 1e-6:
        return None
    scale = 1.0 / span[2]
    return float(scale), float(-minimum[2] * scale)


def flow_draw_scope(height):
    scale, bias = height
    return (
        (
            "local $zzmi_flow_saved_x = x499",
            "local $zzmi_flow_saved_y = y499",
            "local $zzmi_flow_saved_z = z499",
            "pre x499 = 7312401",
            f"pre y499 = {scale:.9g}",
            f"pre z499 = {bias:.9g}",
        ),
        (
            ";MARK:ZZMINativeFlowRestore",
            "pre x499 = $zzmi_flow_saved_x",
            "pre y499 = $zzmi_flow_saved_y",
            "pre z499 = $zzmi_flow_saved_z",
        ),
    )


def flow_shader_sections(tag):
    """Match the native t12 sample, keeping the original code for other draw calls."""
    name = f"ShaderRegexTheHertaZZMINativeHeightFlow_{tag}"
    uv_name = f"ShaderRegexTheHertaZZMIPrimaryMaterialUV_{tag}"
    return (
        f"[{name}]",
        "shader_model = ps_5_0",
        "",
        f"[{name}.Pattern]",
        r"(?<scroll>^[ \t]*mad (?<coord>r\d+)\.xy, cb0\[42\]\.yyyy, cb3\[83\]\.xyxx, \k<coord>\.xyxx\r?\n)",
        r"(?<sample>[ \t]*sample_indexable\(texture2d\)\(float,float,float,float\) \k<coord>\.xyzw, \k<coord>\.xyxx, t12\.xyzw, s6\b)",
        "",
        f"[{name}.Pattern.Replace]",
        "${scroll}",
        r"ld_indexable(texture1d)(float,float,float,float) ${coord}.z, l(499, 0, 0, 0), t120.xxxx\n",
        r"eq ${coord}.z, ${coord}.z, l(7312401.0)\n",
        r"if_nz ${coord}.z\n",
        r"ld_indexable(texture1d)(float,float,float,float) ${coord}.zw, l(499, 0, 0, 0), t120.xxyz\n",
        r"mov ${coord}.x, l(0.500000)\n",
        r"mad_sat ${coord}.y, v3.wwww, ${coord}.zzzz, ${coord}.wwww\n",
        r"mad ${coord}.y, cb0[42].yyyy, cb3[83].yyyy, ${coord}.yyyy\n",
        r"endif\n",
        "${sample}",
        "",
        f"[{name}.InsertDeclarations]",
        "dcl_resource_texture1d (float,float,float,float) t120",
        "",
        f"[{uv_name}]",
        "shader_model = ps_5_0",
        "",
        f"[{uv_name}.Pattern]",
        r"(?<select>^[ \t]*or r0\.z, r0\.z, r0\.y\r?\n)",
        r"(?<sample_uv>[ \t]*movc r0\.zw, r0\.zzzz, v0\.zzzw, v0\.xxxy\r?\n)",
        r"(?=resinfo_indexable\(texture2d\)\(float,float,float,float\) r22\.xy, l\(0\), t50\.xyzw)",
        "",
        f"[{uv_name}.Pattern.Replace]",
        "${select}",
        r"ld_indexable(texture1d)(float,float,float,float) r0.w, l(499, 0, 0, 0), t120.xxxx\n",
        r"eq r0.w, r0.w, l(7312401.0)\n",
        r"if_nz r0.w\n",
        r"mov r0.yz, l(0.0, 0.0, 0.0, 0.0)\n",
        r"endif\n",
        "${sample_uv}",
        "",
        f"[{uv_name}.InsertDeclarations]",
        "dcl_resource_texture1d (float,float,float,float) t120",
    )
