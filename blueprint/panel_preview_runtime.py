"""Render panel previews from temporary exporter output without changing node data."""

import math
import os
import re
import tempfile
import types

from PIL import Image

# Nested exporter collections that fill in their own defaults while an export runs.
# A preview must leave the node exactly as it found it, so every size recorded by
# collect_overlay_state() is restored once the image has been rendered.
_OVERLAY_COLLECTIONS = ("play_group_settings",)


class PreviewNode:
    """Bind exporter methods to an isolated settings overlay over a Blender node."""

    def __init__(self, source):
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "_props", {})

    def __getattr__(self, name):
        if name in self._values:
            return self._values[name]
        value = getattr(self._source, name)
        if isinstance(value, types.MethodType) and value.__self__ is self._source:
            return types.MethodType(value.__func__, self)
        return value

    def __setattr__(self, name, value):
        self._values[name] = value

    def get(self, key, default=None):
        if key in self._props:
            return self._props[key]
        getter = getattr(self._source, "get", None)
        return getter(key, default) if getter is not None else default

    def __getitem__(self, key):
        return self._props[key] if key in self._props else self._source[key]

    def __setitem__(self, key, value):
        self._props[key] = value


def _trim_collection(collection, keep):
    while keep < len(collection):
        index = len(collection) - 1
        if hasattr(collection, "bl_rna"):
            # bpy_prop_collection.remove() takes a position, unlike list.remove().
            collection.remove(index)
        else:
            del collection[index]


def collect_overlay_state(source):
    """Record the collection sizes an export is allowed to grow, then undo it."""
    state = []
    for name in _OVERLAY_COLLECTIONS:
        collection = getattr(source, name, None)
        if collection is None:
            continue
        nested = []
        for index in range(len(collection)):
            intervals = getattr(collection[index], "speed_intervals", None)
            if intervals is not None:
                nested.append((index, len(intervals)))
        state.append((name, len(collection), nested))
    return state


def restore_overlay_state(source, state):
    """Drop the default entries the export filled in, nested collections first."""
    for name, keep, nested in state:
        collection = getattr(source, name, None)
        if collection is None:
            continue
        for index, intervals_keep in nested:
            if index < len(collection):
                try:
                    _trim_collection(collection[index].speed_intervals, intervals_keep)
                except Exception:
                    pass
        try:
            _trim_collection(collection, keep)
        except Exception:
            pass


def export_preview(source, root):
    """Export only this panel and its slider source, independent of sibling panels."""
    node = PreviewNode(source)
    node.ini_file_path = ""
    node.last_mod_ini_path = ""
    slider_source = source._find_shape_ext_node()
    slider = PreviewNode(slider_source) if slider_source is not None else None
    node._find_shape_ext_node = lambda: slider
    emitted_slider = [False]
    node._find_slider_panel_node = lambda: slider if emitted_slider[0] else None
    if slider is not None:
        slider._find_combined_slider_panel = lambda: node
    os.makedirs(os.path.join(root, "res"), exist_ok=True)
    ini_path = os.path.join(root, "preview.ini")
    sections = {"[Constants]": [], "[Present]": []}
    collect_groups = getattr(source, "_collect_custom_material_switch_groups", None)
    if collect_groups is not None:
        glow_groups = [
            (glow, group) for glow, _, group in collect_groups(include_glow_sliders=True)
            if getattr(glow, "bl_idname", "") == "SSMTNode_PostProcess_GlowFX"
        ]
        if len(glow_groups) > 1:
            from .node_postprocess_condition import _iter_upstream_postprocess_nodes

            order = {node.name: -index for index, node in enumerate(_iter_upstream_postprocess_nodes(source))}
            glow_groups.sort(key=lambda item: order.get(item[0].name, -len(order)))
        for glow, group in glow_groups:
            if group is not None:
                sections["[Constants]"].append(f"global persist {group.switch_variable} = 1")
            if node.slider_use_slider_panel and glow.brightness_slider_enabled():
                glow._brightness_slider_value(sections, ini_path, remember_variable=False)
    with open(ini_path, "w", encoding="utf-8") as handle:
        for section, lines in sections.items():
            handle.write("\n".join((section, *lines, "")) + "\n")
    if slider is not None and node.slider_use_slider_panel:
        emitted_slider[0] = bool(slider.execute_postprocess(
            root, _in_place=True, _ini_path=ini_path, _skip_backup=True))
    if not node.execute_postprocess(root, _in_place=True, _ini_path=ini_path, _skip_backup=True):
        if not emitted_slider[0]:
            raise RuntimeError("当前面板没有可预览的按钮或滑块，请检查切换节点与形态键分组")
    return node, ini_path, os.path.join(root, "res")


def _numeric(values, key, default=0.0):
    try:
        return float(values[key])
    except (KeyError, TypeError, ValueError):
        return default


def _apply_glow_slider_defaults(text, values, params):
    variables = set(re.findall(r"(?m)^\s*; GlowFX brightness slider \$([a-z_][a-z_0-9]*)\b", text))
    for index, name in re.findall(r"(?m)^\s*\$param([1-9]\d*)\s*=\s*\$([a-z_][a-z_0-9]*)[ \t]*$", text):
        if name not in variables:
            continue
        index = int(index) - 1
        if index >= len(params):
            params.extend([0.0] * (index + 1 - len(params)))
        params[index] = _numeric(values, name)


def collect_draws(node, values, slider_params):
    """Return texture rectangles in normalized game screen coordinates."""
    draws = []
    ns = node._ensure_namespace()
    has_slider = values.get("base_width0", 0) > 0 and values.get("base_height0", 0) > 0
    independent = values.get(f"{ns}_base_width0", 0) > 0
    slider_x = 0.0

    def add(resource, x, y, width, height):
        if all(math.isfinite(v) for v in (x, y, width, height)) and width > 0 and height > 0:
            draws.append((resource, (x, y, width, height)))

    # Independent panels are placed side by side so both movable panels can be
    # inspected, rather than obscuring each other at their identical default position.
    if independent:
        zoom = values.get(f"{ns}_zoom0", 1.0) or 1.0
        width = values[f"{ns}_base_width0"] * zoom
        height = values[f"{ns}_base_height0"] * zoom
        add(f"ResourceImageToRender0_{ns}", 0.0, 0.0, width, height)
        index = 1
        while f"{ns}_btn_width{index}" in values:
            bw = _numeric(values, f"{ns}_btn_width{index}") * zoom
            bh = _numeric(values, f"{ns}_btn_height{index}") * zoom
            bx = _numeric(values, f"{ns}_fixed_rel_x{index}") * width
            by = _numeric(values, f"{ns}_fixed_rel_y{index}") * height - bh * 0.5
            add(f"ResourceSwapButton{index}_{ns}", bx, by, bw, bh)
            index += 1
        slider_x = width + 0.03

    if not has_slider:
        return draws
    zoom = values.get("zoom0", 1.0) or 1.0
    width = values["base_width0"] * zoom
    height = values["base_height0"] * zoom
    add("ResourceImageToRender0", slider_x, 0.0, width, height)
    geometries = []
    index = 1
    modern = "slider_side_padding" in values
    side = values.get("slider_side_padding", 0.0) * zoom
    gap = values.get("slider_label_gap", 0.01) * zoom
    while f"base_width{index}" in values:
        handle_zoom = zoom if modern else (values.get(f"zoom{index}", 1.0) or 1.0)
        hw = values[f"base_width{index}"] * handle_zoom
        hh = values[f"base_height{index}"] * handle_zoom
        label_scale = zoom if modern else 1.0
        lw = values.get(f"btn_base_width{index}", values.get(f"btn_width{index}", 0.0)) * label_scale
        lh = values.get(f"btn_base_height{index}", values.get(f"btn_height{index}", hh)) * label_scale
        left = side + lw + gap if modern else lw + 0.02
        right = width - side if modern else width * 0.95
        maximum = max(left, right - hw)
        param = slider_params[index - 1] if index <= len(slider_params) else 0.0
        hx = slider_x + left + max(0.0, min(1.0, param)) * (maximum - left)
        hy = values.get(f"fixed_rel_y{index}", 0.5) * height - hh * 0.5
        center = hx + hw * 0.5
        geometries.append((index, (hx, hy, hw, hh),
                           (slider_x + left - lw - gap, hy + (hh - lh) * 0.5, lw, lh)))
        add("ResourceLeftBar", slider_x + left, hy + hh * 0.25,
            center - slider_x - left, hh * 0.5)
        add("ResourceRightBar", center, hy + hh * 0.25, slider_x + right - center, hh * 0.5)
        index += 1
    for index, _handle, label in geometries:
        add(f"ResourcePlayPauseButton{index}", *label)
    for _index, handle, _label in geometries:
        add("ResourceSliderHandle", *handle)

    widths, heights = [], []
    while f"{ns}_btn_w{len(widths) + 1}" in values:
        i = len(widths) + 1
        widths.append(_numeric(values, f"{ns}_btn_w{i}"))
        heights.append(_numeric(values, f"{ns}_btn_h{i}"))
    if widths:
        attached = node.panel_mode == "ATTACHED_LEFT"
        per_row = max(1, min(int(node.buttons_per_row or 1), len(widths)))
        rows = math.ceil(len(widths) / per_row)
        columns = [max(widths[c::per_row]) for c in range(per_row)]
        row_gap = float(node.button_row_spacing)
        col_gap = float(node.button_column_spacing)
        for i, (bw, bh) in enumerate(zip(widths, heights)):
            row, col = divmod(i, per_row)
            if attached:
                bx = slider_x - 0.02 - bw * zoom
                by = (float(node.button_top_padding) + i * (bh + row_gap)) * zoom
            else:
                bx = slider_x + (0.02 + sum(columns[:col]) + col * col_gap
                                 + (columns[col] - bw) * 0.5) * zoom
                if node.panel_mode == "INTEGRATED_TOP":
                    by = (float(node.button_top_padding) + row * (bh + row_gap)) * zoom
                else:
                    by = height - (rows - row) * (bh + row_gap) * zoom
            add(f"ResourceSwapButton{i + 1}_{ns}", bx, by, bw * zoom, bh * zoom)
    return draws


def paste_clipped(canvas, image, x, y):
    """Skip fully offscreen images before calling Pillow's crop."""
    left, top = max(0, -x), max(0, -y)
    right = min(image.width, canvas.width - x)
    bottom = min(image.height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    canvas.alpha_composite(image.crop((left, top, right, bottom)), (x + left, y + top))


def render_panel_runtime_image(node, ini_path, res_dir):
    from . import node_postprocess_swap_panel as panel
    with open(ini_path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    values = panel._panel_runtime_parse_globals(text)
    resources = panel._panel_runtime_parse_resources(text)
    params = panel._panel_runtime_slider_params(node._find_shape_ext_node(), values)
    _apply_glow_slider_defaults(text, values, params)
    draws = collect_draws(node, values, params)
    if not draws:
        raise RuntimeError("当前节点未生成可渲染的面板，请检查按钮列表和滑块分组")
    xmin = min(rect[0] for _, rect in draws)
    ymin = min(rect[1] for _, rect in draws)
    xmax = max(rect[0] + rect[2] for _, rect in draws)
    ymax = max(rect[1] + rect[3] for _, rect in draws)
    span_x = (xmax - xmin) * (16.0 / 9.0)
    span_y = ymax - ymin
    density = min(1080.0, 2048.0 / max(span_x, span_y, 1e-8))
    sx, sy = density * (16.0 / 9.0), density
    margin = 24
    canvas = Image.new("RGBA", (max(1, math.ceil(span_x * density)) + margin * 2,
                                 max(1, math.ceil(span_y * density)) + margin * 2), (30, 30, 30, 255))
    missing = set()
    for resource, (x, y, width, height) in draws:
        filename = resources.get(resource, "")
        path = filename if os.path.isabs(filename) else os.path.join(res_dir, filename)
        if not filename or not os.path.isfile(path):
            if resource not in missing:
                missing.add(resource)
                print(f"[面板预览] 缺少资源 {resource}（{path or '未在 ini 中登记'}），该元素不出现在预览图里")
            continue
        with Image.open(path) as source:
            image = source.convert("RGBA")
        size = (max(1, round(width * sx)), max(1, round(height * sy)))
        # Enlarged UI textures get bilinear sampling like the game's draw call does;
        # Lanczos ringing on a two-pixel outline reads as a preview artifact.
        filter_kind = (Image.Resampling.LANCZOS if size[0] <= image.width and size[1] <= image.height
                       else Image.Resampling.BILINEAR)
        image = image.resize(size, filter_kind)
        paste_clipped(canvas, image, margin + round((x - xmin) * sx), margin + round((y - ymin) * sy))
    return canvas


def _preview_directory():
    directory = os.path.join(tempfile.gettempdir(), "ssmt_panel_previews")
    os.makedirs(directory, exist_ok=True)
    return directory


def generate_preview(node):
    slider_source = node._find_shape_ext_node()
    sources = [node] if slider_source is None else [node, slider_source]
    states = [(source, collect_overlay_state(source)) for source in sources]
    try:
        with tempfile.TemporaryDirectory(prefix="ssmt_panel_runtime_preview_") as root:
            proxy, ini_path, res_dir = export_preview(node, root)
            image = render_panel_runtime_image(proxy, ini_path, res_dir)
    finally:
        for source, state in states:
            restore_overlay_state(source, state)
    # One stable file per panel: sibling panels keep their own preview, and a repeat
    # preview overwrites its own image instead of filling the temp directory.
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (proxy._ensure_namespace() or "panel"))
    output = os.path.join(_preview_directory(), f"{name}.png")
    image.convert("RGB").save(output)
    return output
