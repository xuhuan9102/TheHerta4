"""Glow FX着色器后处理节点。

该节点复用 RabbitFX Pro 的目标列表和面板属性，但导出时把目标的 RabbitFX
发光调用替换为随 Mod 分发的 ``GlowFX`` 专用着色器调用。
原始绘制、材质绑定和 ``ZZMI\\SetTextures`` 保持不变。
"""

import bpy
import glob
import hashlib
import json
import os
import re
import shutil
import uuid
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from .node_postprocess_rabbitfx_pro import (
    ini_scope,
    SSMTNode_PostProcess_RabbitFXPro,
    _format_float,
    build_breath_parameter_lines,
    _insert_block,
    _leading_whitespace,
    _load_material_module,
    _normalize_suffix,
    _on_preview_parameter_update,
    _objects_used_by_other_nodes,
    _glow_previews,
    _preview_tree_key,
    _section_fallback_unit,
    _strip_previous_blocks,
    _unit_edit_scope,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base
from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.ttl_protocol import parse_efmi_ttl_draw_call, parse_ttl_draw_call
from .node_postprocess_rabbitfx import (
    _extract_hash_from_object,
    _extract_resource_suffix,
)

NODE_IDNAME = "SSMTNode_PostProcess_GlowFX"
RUN_LINE = "run = CommandListGlowFXRun"
_EFMI_GLOW_RUN = "run = CommandListEFMIGlowFXRun"
_EFMI_GLOW_PASS = "vs == 73414 && o0 !== null && o2 === null && oD !== null"
_EFMI_DRAW_PARAMS = ("count", "instances", "start", "base", "first_instance")
_TTL_DRAW_LINE = r"run = CommandList\TTL\Draw".casefold()
_EFMI_TTL_DRAW_LINE = r"run = CommandList\EFMITTL\Draw".casefold()
_DRAW_GUARD = 'if !$\\SlotFix\\is_outline_shader'
GLOW_RESOURCE = "ResourceGlowFXMap"
_DEFAULT_HALO_RADIUS = 40
_LOCAL_GLOW_REF_RE = re.compile(r"^ResourceGlowFXMap(?:_r\d+)?\s*=\s*ref\s+(\S+)", re.IGNORECASE)
_RUNTIME_NAME_RE = re.compile(r"\b(?:\w*GeoGlow\w*|ResourceGlowFXMap|CommandListGlowFXRun)\b", re.IGNORECASE)
_LEGACY_RUN_LINE = r"run = CommandList\GlowFX\Run"
_LEGACY_GLOW_REF_RE = re.compile(r"^Resource\\GlowFX\\Glowmap\s*=\s*ref\s+(\S+)", re.IGNORECASE)
_EFMI_GLOW_REF_RE = re.compile(r"^Resource\\EFMITTL\\GlowFXMap\s*=\s*ref\s+(\S+)", re.IGNORECASE)


_GLOW_VAR_RE = re.compile(r"^\$glowfx_[a-z0-9_]+$")


def _glowfx_param(name, tag=None):
    """GlowFX 工作变量；给了 tag 就是该节点实例的 [Constants] 参数。

    ``$glowfx_h`` 是共享 CommandList 读的工作变量；``$glowfx_<tag>_h`` 是
    面板值，只在 [Constants] 声明一次，draw 段做「变量 = 变量」。
    """
    param = re.sub(r"[^a-z0-9_]", "_", str(name).lower())
    if tag is None:
        return f"$glowfx_{param}"
    safe_tag = re.sub(r"[^a-z0-9_]", "_", str(tag).lower()).strip("_") or "0"
    variable = f"$glowfx_{safe_tag}_{param}"
    if not _GLOW_VAR_RE.fullmatch(variable):
        raise ValueError(f"Glow FX 参数变量名非法：{variable}")
    return variable


def _halo_normalization(radius):
    spacing = 2 * max(1, (radius + 95) // 96)
    total = 1.0
    for offset in range(spacing, radius, spacing):
        fraction = offset / radius
        edge = 1.0 - fraction * fraction * (3.0 - 2.0 * fraction)
        total += 2.0 * 2.0 ** (-4.0 * fraction * fraction) * edge
    return total


_PARAM_BEGIN = "; === GlowFX Params ==="
_PARAM_END = "; === End GlowFX Params ==="
_BREATH_BEGIN = "; === GlowFX Breath ==="
_BREATH_END = "; === End GlowFX Breath ==="
_CALL_BEGIN = "; === GlowFX Call ==="
_CALL_END = "; === End GlowFX Call ==="
_RESET_BEGIN = "; === GlowFX Reset ==="
_RESET_END = "; === End GlowFX Reset ==="

_VAR_PREFIX = "$glowfx_"
_GLOW_GLOBAL_RE = re.compile(
    r"^\s*global\s+(?:persist\s+)?\$glowfx_", re.IGNORECASE
)
_GLOW_PARAM_NAMES = (
    "h", "s", "v", "brightness", "interpolate", "only", "radius", "normalization",
)
_VAR_IF_RE = re.compile(r"^if\s+\$glowfx_", re.IGNORECASE)
_IF_LINE_RE = re.compile(r"^if\s+", re.IGNORECASE)
_DRAWINDEXED_RE = re.compile(
    r"^(?P<indent>\s*)drawindexed\s*=\s*(?P<args>[^;]+)",
    re.IGNORECASE,
)
_DRAWINDEXEDINSTANCED_RE = re.compile(r"^drawindexedinstanced\s*=\s*([^;]+)", re.IGNORECASE)
_RABBITFX_PARAM_RE = re.compile(
    r"^\$\\RabbitFX\\(?:h|s|v|brightness|interpolate)\s*=",
    re.IGNORECASE,
)
_RABBITFX_ELEMENT_RE = re.compile(r"RabbitFX", re.IGNORECASE)


class _RabbitFXProPanelMixin:
    """复用 RabbitFX Pro 的 Python 面板方法，不继承已注册的 Blender Node。"""


for _name, _value in SSMTNode_PostProcess_RabbitFXPro.__dict__.items():
    if _name.startswith("bl_") or _name == "is_registered_node_type":
        continue
    if callable(_value) or isinstance(_value, property):
        setattr(_RabbitFXProPanelMixin, _name, _value)


def _marker_kind(line):
    text = str(line or "").strip()
    if text.casefold() in {_PARAM_BEGIN.casefold(), _CALL_BEGIN.casefold()}:
        return "start"
    if text.casefold() in {_PARAM_END.casefold(), _CALL_END.casefold()}:
        return "end"
    return ""


def _strip_previous_glowfx_blocks(lines):
    kept = []
    index = 0
    total = len(lines)
    while index < total:
        text = str(lines[index]).strip()
        if _marker_kind(text) == "start":
            end_index = next(
                (
                    offset
                    for offset in range(index + 1, total)
                    if _marker_kind(str(lines[offset]).strip()) == "end"
                ),
                None,
            )
            if end_index is not None:
                index = end_index + 1
                continue
        if (
            text.casefold().startswith("resource\\glowfx\\")
            or text.casefold().startswith("$\\glowfx\\")
            or text.casefold() == _LEGACY_RUN_LINE.casefold()
        ):
            index += 1
            continue
        kept.append(lines[index])
        index += 1
    return kept


def _strip_rabbitfx_glow_lines(lines, profile):
    kept = []
    for line in lines:
        text = str(line).strip()
        if profile.glow_ref_re.match(text):
            continue
        if profile.run_line_re.match(text):
            continue
        if _RABBITFX_PARAM_RE.match(text):
            continue
        kept.append(line)
    return kept


def _strip_material_rabbitfx_lines(lines, keep_markers=()):
    """删除材质转资源写入的 RabbitFX 行、标记与变量。"""
    return [
        line
        for line in lines
        if _RABBITFX_ELEMENT_RE.search(str(line)) is None or str(line).strip() in keep_markers
    ]


def _strip_material_rabbitfx_sections(sections, keep_markers=()):
    cleaned = OrderedDict()
    for section_name, section_lines in sections.items():
        if _RABBITFX_ELEMENT_RE.search(str(section_name)):
            continue
        cleaned[section_name] = _strip_material_rabbitfx_lines(section_lines, keep_markers)
    return cleaned


def _strip_material_rabbitfx_text(text):
    return "".join(
        line
        for line in str(text or "").splitlines(keepends=True)
        if _RABBITFX_ELEMENT_RE.search(line) is None
    )


def _record_resource_ref(record, profile):
    for line in record.get("lines", ()) or ():
        match = profile.glow_ref_re.match(str(line).strip())
        if match:
            name = match.group("name").strip()
            if name.casefold() != "null":
                return name
    return ""


def _block_resource_ref(lines, profile):
    for line in lines:
        match = profile.glow_ref_re.match(str(line).strip())
        if match:
            name = match.group("name").strip()
            if name.casefold() != "null":
                return name
    return ""


def _draw_arguments(line):
    match = _DRAWINDEXED_RE.match(str(line or "").strip())
    if match is None:
        return None
    args = [part.strip() for part in match.group("args").split(",")]
    if len(args) != 3 or any(not part for part in args):
        return None
    return args


def _glow_draws(lines, efmi=False):
    for index, line in enumerate(lines):
        text = str(line).partition(";")[0].strip()
        command = text.casefold()
        if command == _TTL_DRAW_LINE:
            parsed = parse_ttl_draw_call(lines, index)
            if parsed is not None:
                start, args = parsed
                yield start, index, args, "TTL"
        elif command == _EFMI_TTL_DRAW_LINE:
            parsed = parse_efmi_ttl_draw_call(lines, index)
            if parsed is not None:
                start, args = parsed
                yield start, index, (*args, 0), "EFMITTL"
        else:
            if efmi and (match := _DRAWINDEXEDINSTANCED_RE.match(text)):
                args = [part.strip() for part in match.group(1).split(",")]
                if len(args) == len(_EFMI_DRAW_PARAMS) and all(args):
                    yield index, index, args, "EFMI"
                continue
            args = _draw_arguments(line)
            if args is not None:
                if efmi:
                    count, start, base = args
                    yield index, index, (count, "1", start, base, "0"), "EFMI"
                else:
                    yield index, index, args, None


def _surface_mode_get(node):
    # The original experimental replay item was stored as enum value 2.
    return 1 if node.get("native_surface_mode") == 1 else 0


def _surface_mode_set(node, value):
    if node.get("native_surface_mode") == 2 and "native_surface_replay" not in node:
        node["native_surface_replay"] = True
    node["native_surface_mode"] = value


def _surface_replay_get(node):
    return bool(node.get("native_surface_replay", node.get("native_surface_mode") == 2))


def _surface_replay_set(node, value):
    node["native_surface_replay"] = bool(value)


def _update_effect_switch_variable(node, _context):
    if node.effect_switch_enabled:
        variable = node._effect_switch_variable()
        if variable != node.effect_switch_variable:
            node.effect_switch_variable = variable


class SSMTNode_PostProcess_GlowFX(
    _RabbitFXProPanelMixin,
    SSMTNode_PostProcess_Base,
):
    __annotations__ = dict(SSMTNode_PostProcess_RabbitFXPro.__annotations__)
    enable_native_surface_flow: bpy.props.BoolProperty(
        name="独立表面流光",
        description="叠加随镜头和曲面变化的动态镜面反光，保留游戏原生反光",
        default=False,
    )
    native_surface_color: bpy.props.FloatVectorProperty(
        name="流光颜色",
        subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.0, 0.75, 0.62),
    )
    native_surface_use_texture: bpy.props.BoolProperty(
        name="使用流光贴图", description="由 DDS 贴图的 RGB 和透明度控制独立流光", default=False,
    )
    native_surface_texture: bpy.props.StringProperty(
        name="流光贴图", subtype='FILE_PATH',
        description="选择自定义 DDS，留空时使用插件内置流光贴图",
    )
    native_surface_mode: bpy.props.EnumProperty(
        name="流光模式",
        items=[
            ('MIRROR', "镜面流光", "高光随镜头和曲面的镜面反射方向移动", 0),
            ('HEIGHT', "高光流光", "反光按几何高度从脚向头滚动", 1),
        ],
        get=_surface_mode_get, set=_surface_mode_set,
    )
    native_surface_replay: bpy.props.BoolProperty(
        name="独立重绘",
        description="用 Mod 自己的着色器重绘所选流光模式",
        get=_surface_replay_get, set=_surface_replay_set,
    )
    native_surface_speed: bpy.props.FloatProperty(
        name="流光速度",
        description="流光每秒循环的次数",
        default=0.25, min=0.01, max=4.0,
    )
    native_surface_brightness: bpy.props.FloatProperty(
        name="流光亮度",
        description="动态反光强度，1 为参考强度",
        default=1.0, min=0.0, max=4.0,
    )
    surface_brightness_slider: bpy.props.BoolProperty(name="游戏内亮度滑块", default=False)
    surface_brightness_variable: bpy.props.StringProperty(options={'HIDDEN'})
    surface_brightness_icon_mode: bpy.props.EnumProperty(
        name="滑块按钮图标",
        items=[
            ('REMARK', "用备注生成图标", ""),
            ('IMAGE', "使用自定义图片", ""),
            ('DEFAULT', "使用面板默认图片", ""),
        ],
        default='REMARK',
    )
    surface_brightness_icon_remark: bpy.props.StringProperty(name="滑块按钮备注")
    surface_brightness_icon_image: bpy.props.StringProperty(name="滑块按钮图片", subtype='FILE_PATH')
    surface_brightness_icon_font: bpy.props.EnumProperty(
        name="字体",
        items=[
            ('msyh.ttc', "微软雅黑", ""),
            ('simsun.ttc', "宋体", ""),
            ('simhei.ttf', "黑体", ""),
            ('arial.ttf', "Arial", ""),
        ],
        default='msyh.ttc',
    )
    surface_brightness_icon_font_size: bpy.props.IntProperty(name="字号大小", default=36, min=10, max=300)
    surface_brightness_icon_text_color: bpy.props.FloatVectorProperty(
        name="文字颜色", subtype='COLOR', size=3, default=(1.0, 1.0, 1.0), min=0.0, max=1.0,
    )
    surface_brightness_icon_stroke_color: bpy.props.FloatVectorProperty(
        name="描边颜色", subtype='COLOR', size=3, default=(0.0, 0.0, 0.0), min=0.0, max=1.0,
    )
    surface_brightness_icon_stroke_width: bpy.props.IntProperty(name="描边宽度", default=4, min=0, max=20)
    effect_switch_enabled: bpy.props.BoolProperty(
        name="游戏内切换", default=False, update=_update_effect_switch_variable,
    )
    effect_switch_variable: bpy.props.StringProperty(
        name="切换变量", description="启用时自动分配，重名时改用未占用的变量",
        update=_update_effect_switch_variable,
    )
    effect_switch_key: bpy.props.StringProperty(
        name="切换按键",
        description="留空时仅通过变量或切换面板控制",
        default="",
    )
    effect_switch_comment: bpy.props.StringProperty(name="切换备注", description="切换面板显示名称")

    __annotations__["glow_brightness"] = bpy.props.FloatProperty(
        name="发光强度",
        description="调整发光亮度",
        default=8.0, min=0, max=500, update=_on_preview_parameter_update,
    )
    halo_radius: bpy.props.IntProperty(
        name="光晕范围",
        description="光晕在渲染画面中的渐隐半径，单位为像素，0 仅保留发光核心",
        default=_DEFAULT_HALO_RADIUS, min=0,
    )

    emission_only: bpy.props.BoolProperty(
        name="仅绘制发光贴图",
        description="隐藏目标网格的原材质，只绘制发光贴图覆盖的区域",
        default=False,
    )

    bl_idname = NODE_IDNAME
    bl_label = "Glow FX着色器"
    bl_description = (
        "复用 RabbitFX 面板与目标贴图选择，把发光绘制交给 "
        "GlowFX 专用着色器；不会调用 RabbitFX 内核"
    )

    def init(self, context):
        SSMTNode_PostProcess_Base.init(self, context)
        if not self.buffer_tag:
            self.buffer_tag = uuid.uuid4().hex[:8]
        self.width = 380

    def copy(self, source):
        self.buffer_tag = uuid.uuid4().hex[:8]
        self.surface_brightness_variable = ""
        if getattr(self, "effect_switch_enabled", False):
            self.effect_switch_variable = ""

    def _var_tag(self):
        tag = super()._var_tag().lower()
        nodes = getattr(getattr(self, "id_data", None), "nodes", ())
        tags_by_name = {
            node.name: _RabbitFXProPanelMixin._var_tag(node).lower()
            for node in nodes
            if getattr(node, "bl_idname", "") == NODE_IDNAME
        }
        matching_names = sorted(name for name, value in tags_by_name.items() if value == tag)
        if self.name not in matching_names or self.name == matching_names[0]:
            return tag

        # Saved copies can share a tag. Assign stable suffixes without editing the tree.
        reserved_tags = set(tags_by_name.values())
        suffix = 2
        for name in matching_names[1:]:
            while f"{tag}_{suffix}" in reserved_tags:
                suffix += 1
            if name == self.name:
                return f"{tag}_{suffix}"
            suffix += 1

    @property
    def fx_profile_label(self):
        return "GlowFX 专用内核（随 Mod 加载）"

    def draw_buttons(self, context, layout):
        self._draw_targets(layout)
        layout.prop(self, self._button_remark_property(), text="备注")
        layout.prop(self, "effect_switch_enabled")
        if getattr(self, "effect_switch_enabled", False):
            box = layout.box()
            row = box.row(align=True)
            row.prop(self, "effect_switch_key")
            op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
            op.node_name = self.name
            op.node_tree_name = self.id_data.name
            op.target_prop = "effect_switch_key"
            box.prop(self, "effect_switch_variable")
        self._draw_brightness_slider(layout)
        layout.prop(self, "enable_native_surface_flow")
        if getattr(self, "enable_native_surface_flow", False):
            layout.prop(self, "native_surface_replay")
            layout.prop(self, "native_surface_mode")
            replay = self.native_surface_replay
            if not replay:
                layout.prop(self, "native_surface_use_texture")
            if self.native_surface_use_texture and not replay:
                layout.prop(self, "native_surface_texture")
            else:
                layout.prop(self, "native_surface_color")
            layout.prop(self, "native_surface_speed")
            layout.prop(self, "native_surface_brightness")
            return
        self._draw_glow_block(layout)
        self._draw_fx_block(layout)
        layout.separator()
        layout.label(text=self.fx_profile_label, icon='INFO')
        layout.prop(self, "enable_reset")
        row = layout.row(align=True)
        row.prop(self, "preview_use_color", text="")
        colors = row.row(align=True)
        colors.enabled = self.preview_use_color
        colors.prop(self, "preview_color", text="预览底色")
        op = row.operator(
            "ssmt.rabbitfx_pro_preview",
            text="退出预览" if _preview_tree_key(self) in _glow_previews else "预览发光",
            icon='HIDE_OFF',
        )
        op.node_name = self.name

    def _draw_brightness_slider(self, layout):
        layout.prop(self, "surface_brightness_slider")
        if not self.surface_brightness_slider:
            return
        box = layout.box()
        box.label(text="游戏内亮度比例 0%–100%", icon='INFO')
        if not self.enable_native_surface_flow and self._breathing():
            box.label(text="调节呼吸灯最大强度", icon='INFO')
        box.prop(self, "surface_brightness_icon_mode")
        if self.surface_brightness_icon_mode == 'REMARK':
            style = box.box()
            style.label(text="文字图标样式", icon='FILE_FONT')
            style.prop(self, "surface_brightness_icon_font")
            style.prop(self, "surface_brightness_icon_font_size")
            colors = style.row(align=True)
            colors.prop(self, "surface_brightness_icon_text_color")
            colors.prop(self, "surface_brightness_icon_stroke_color")
            style.prop(self, "surface_brightness_icon_stroke_width")
        elif self.surface_brightness_icon_mode == 'IMAGE':
            box.prop(self, "surface_brightness_icon_image")

    def _draw_breath_block(self, layout):
        layout.prop(self, "halo_radius")
        layout.prop(self, "emission_only")
        super()._draw_breath_block(layout)

    def _halo_suffix(self):
        return "" if self.halo_radius == _DEFAULT_HALO_RADIUS else f"_r{self.halo_radius}"

    def brightness_slider_enabled(self):
        return getattr(self, "surface_brightness_slider", False) and (
            getattr(self, "enable_native_surface_flow", False) or self.enable_glow
        )

    def brightness_slider_label(self):
        if getattr(self, "enable_native_surface_flow", False):
            return "流光亮度"
        return "呼吸灯最大亮度" if self._breathing() else "发光亮度"

    def _button_remark_property(self):
        if self.effect_switch_comment.strip() and not self.is_property_set("surface_brightness_icon_remark"):
            return "effect_switch_comment"
        return "surface_brightness_icon_remark"

    def brightness_slider_remark(self):
        return getattr(self, self._button_remark_property()).strip()

    def _brightness_slider_value(self, sections=None, ini_file=None, *, remember_variable=True):
        if getattr(self, "enable_native_surface_flow", False):
            initial = self.native_surface_brightness
        else:
            initial = self.breath_brightness_max if self._breathing() else self.glow_brightness
        if not self.brightness_slider_enabled():
            return initial
        from .variable_registry import glow_fx_switch_variable_name

        variable = glow_fx_switch_variable_name(
            self, f"glowfx_brightness_{self._var_tag()}", "surface_brightness_variable",
        )
        if sections is not None:
            mode = self.surface_brightness_icon_mode
            remark = self.brightness_slider_remark()
            icon = {"mode": mode, "group": remark}
            switch = self._effect_switch_variable()
            if switch:
                icon["switch"] = switch
            if mode == 'REMARK':
                icon["remark"] = remark or self.brightness_slider_label()
                icon["style"] = {
                    "font_family": self.surface_brightness_icon_font,
                    "font_size": self.surface_brightness_icon_font_size,
                    "text_color": tuple(self.surface_brightness_icon_text_color),
                    "stroke_color": tuple(self.surface_brightness_icon_stroke_color),
                    "stroke_width": self.surface_brightness_icon_stroke_width,
                }
            elif mode == 'IMAGE':
                selected = self.surface_brightness_icon_image
                source = Path(bpy.path.abspath(selected, library=getattr(self.id_data, "library", None)))
                if not selected or not source.is_file():
                    raise ValueError("Glow FX 滑块按钮图片不存在，请重新选择")
                data = source.read_bytes()
                resource = Path("res") / f"glow-slider-{hashlib.sha256(data).hexdigest()[:16]}{source.suffix.lower()}"
                dest = Path(ini_file).parent / resource
                dest.parent.mkdir(exist_ok=True)
                dest.write_bytes(data)
                icon["image"] = resource.as_posix()
            if remember_variable:
                self.surface_brightness_variable = variable
            sections.setdefault("[Constants]", []).extend([
                f"; GlowFX brightness slider {variable} {json.dumps(icon)}",
                f"global persist {variable} = 1.000000",
            ])
        return f"{_format_float(initial)} * {variable}"

    def effect_switch_group(self):
        if not getattr(self, "effect_switch_enabled", False):
            return None
        from .variable_registry import glow_fx_switch_variable_name

        variable = glow_fx_switch_variable_name(self, f"glowfx_enabled_{self._var_tag()}")
        comment = self.brightness_slider_remark()
        if not comment:
            comment = self.brightness_slider_label() if self.brightness_slider_enabled() else self.label or self.name
        return SimpleNamespace(
            switch_variable=variable,
            key=self.effect_switch_key.strip(),
            comment=comment,
            state_count=2,
        )

    def _effect_switch_variable(self):
        group = self.effect_switch_group()
        return group.switch_variable if group else ""

    def _emit_effect_switch(self, sections):
        group = self.effect_switch_group()
        if group is None:
            return
        from .node_postprocess_custom_material_assign import find_activation_flag_in_sections
        from .node_postprocess_material import resolve_keyswap_key

        variable = group.switch_variable
        constants = sections.setdefault("[Constants]", [])
        existing = rf"^\s*global\s+(?:persist\s+)?{re.escape(variable)}\s*="
        if not any(re.match(existing, line, re.IGNORECASE) for line in constants):
            constants.append(f"global persist {variable} = 1")
        if group.key:
            name = f"[KeySwap_GlowFX_{variable[1:]}]"
            activation = find_activation_flag_in_sections(sections)
            key = resolve_keyswap_key(sections, name, group.key, log_prefix="Glow FX")
            sections[name] = [
                f"; {group.comment}",
                *([f"condition = {activation} == 1"] if activation else []),
                f"key = {key}", "type = cycle", f"{variable} = 0,1",
            ]

    def validate_export_configuration(self):
        variable = self._effect_switch_variable()
        if variable and not re.fullmatch(r"\$[a-z_][a-z_0-9]*", variable):
            raise ValueError("Glow FX 切换变量须以英文字母或下划线开头，只能包含英文字母、数字和下划线")
        if variable and variable != self.effect_switch_variable:
            self.effect_switch_variable = variable
        if getattr(self, "enable_native_surface_flow", False):
            if GlobalConfig.logic_name != LogicName.ZZMI:
                raise ValueError("独立表面流光需要 ZZMI（DX11）导出预设")
        conflicts = []
        for item in self.target_items:
            obj = item.target_object
            if obj is None:
                continue
            owners = _objects_used_by_other_nodes(self, obj)
            if owners:
                conflicts.append(f"{obj.name}（也被 {owners} 接管）")
        if conflicts:
            raise ValueError(
                "Glow FX着色器：同一物体被多个发光后处理节点重复接管："
                + "、".join(conflicts)
            )

    def execute_postprocess(self, mod_export_path):
        if bool(getattr(self, "mute", False)):
            return
        targets = [
            item.target_object
            for item in self.target_items
            if item.target_object is not None
        ]
        if not targets:
            print("[GlowFX] 未指定任何目标物体")
            return
        if self.enable_sync:
            print("[GlowFX] 专用内核没有 W-Engine 同步，本次跳过")
        if self.enable_colorshift:
            print("[GlowFX] 专用内核没有 ColorShift，本次跳过")

        material_module = _load_material_module()
        output_path = os.path.normcase(os.path.abspath(mod_export_path))
        registered = [
            (ini_file, records)
            for ini_file, records in material_module._rabbitfx_outputs.items()
            if os.path.dirname(ini_file) == output_path
        ]

        generated = False
        if registered:
            for ini_file, records in registered:
                self._create_cumulative_backup(ini_file, mod_export_path)
                generated |= self._process_ini_file(
                    ini_file,
                    targets,
                    records,
                )
        else:
            for ini_file in glob.glob(os.path.join(mod_export_path, "*.ini")):
                self._create_cumulative_backup(ini_file, mod_export_path)
                generated |= self._process_ini_file(
                    ini_file,
                    targets,
                    None,
                )

        if generated:
            self._copy_glow_fx_resources(mod_export_path)
            self._disable_legacy_glow_fx_ini(mod_export_path)

    def _has_sibling_rabbitfx_targets(self):
        """同蓝图里是否有另一套 RabbitFXPro 节点、且它还配置了目标物体。"""
        tree = getattr(self, "id_data", None)
        for other in getattr(tree, "nodes", None) or []:
            if other is self:
                continue
            if getattr(other, "bl_idname", "") != "SSMTNode_PostProcess_RabbitFXPro":
                continue
            if bool(getattr(other, "mute", False)):
                continue
            for item in getattr(other, "target_items", None) or []:
                if getattr(item, "target_object", None) is not None:
                    return True
        return False

    def _sibling_glow_markers(self, records):
        targets = {
            item.target_object.name
            for other in getattr(getattr(self, "id_data", None), "nodes", ())
            if other is not self and getattr(other, "bl_idname", "") == NODE_IDNAME
            and not getattr(other, "mute", False)
            for item in other.target_items
            if item.target_object is not None
        }
        # The next GlowFX node uses these comments to locate its material draws.
        return {
            record["marker"] for record in records or ()
            if record.get("type") == "Glowmap" and record.get("object") in targets
        }

    def _process_ini_file(self, ini_file, targets, records=None):
        self._glowfx_efmi_used = False
        with open(ini_file, "r", encoding="utf-8") as handle:
            content = handle.read()

        preserved_driver, content = self.split_anim_driver_block_content(content)
        content, tail = self.split_auto_appended_tail_content(content)
        sections, preamble = self._parse_ini(content)

        native_surface = getattr(self, "enable_native_surface_flow", False)
        if native_surface:
            changed = self._apply_native_surface_flow(sections, targets, ini_file)
            generated = False
        elif records is None:
            changed, generated = self._apply_fallback_injections(sections, targets)
        else:
            changed = self._apply_material_records(sections, records, targets, "")
            generated = changed

        if not changed:
            return False
        if not native_surface and generated and self.brightness_slider_enabled():
            self._brightness_slider_value(sections, ini_file)
        self._emit_effect_switch(sections)
        if generated:
            run = (RUN_LINE + self._halo_suffix()).casefold()
            commands = {line.strip().casefold() for lines in sections.values() for line in lines}
            efmi_replay = _EFMI_GLOW_RUN.casefold() in commands
            if run in commands:
                self._embed_glow_runtime(sections)
            elif not efmi_replay:
                constants = sections.setdefault("[Constants]", [])
                for name, default in (("h", 0), ("s", 0), ("v", 0), ("brightness", 1), ("interpolate", 0)):
                    declaration = f"global $glowfx_{name} = {default}"
                    if declaration not in constants:
                        constants.append(declaration)
            if efmi_replay:
                self._embed_efmi_glow_runtime(sections, os.path.dirname(ini_file))
            if self.enable_glow:
                self._ensure_glow_parameter_constants(sections)
                if self._breathing():
                    self._ensure_glow_constants(sections)
                    self._ensure_breath_clock(sections)
            if self._has_sibling_rabbitfx_targets():
                # 同蓝图还有 RabbitFXPro 在用：只清本节点自己的单元，别动别人的运行元素。
                print(
                    "[GlowFX] 同蓝图存在 RabbitFXPro 目标，保留其 RabbitFX 运行元素"
                )
            else:
                sections = _strip_material_rabbitfx_sections(
                    sections, self._sibling_glow_markers(records)
                )
                preamble = _strip_material_rabbitfx_lines(preamble)
                preserved_driver = _strip_material_rabbitfx_text(preserved_driver)
                tail = _strip_material_rabbitfx_text(tail)

        with open(ini_file, "w", encoding="utf-8") as handle:
            if preserved_driver:
                handle.write(preserved_driver)
                if not preserved_driver.endswith("\n"):
                    handle.write("\n")
                handle.write("\n")
            for line in preamble:
                handle.write(line + "\n")
            for section_name, section_lines in sections.items():
                if section_name.startswith("["):
                    handle.write(f"{section_name}\n")
                for line in section_lines:
                    handle.write(f"{line}\n")
                handle.write("\n")
            if tail:
                handle.write(tail)
        print(f"[GlowFX] 已写入 {os.path.basename(ini_file)}")
        return generated

    def _apply_material_records(self, sections, records, objects, _buffer_name):
        names = {obj.name for obj in objects}
        profile = self.fx_profile
        grouped = OrderedDict()
        for record in records:
            if record.get("type") != "Glowmap":
                continue
            if record.get("object") not in names:
                continue
            section = sections.get(record.get("section"))
            if section is None:
                continue
            marker = record.get("marker")
            try:
                marker_index = next(
                    index
                    for index, line in enumerate(section)
                    if str(line).strip() == marker
                )
            except StopIteration:
                continue
            units = [
                unit
                for unit in ini_scope.iter_draw_units(section)
                if unit["start"] <= marker_index < unit["end"]
            ]
            unit = (
                max(units, key=lambda item: item["start"])
                if units
                else _section_fallback_unit(section)
            )
            key = (record["section"], int(unit["start"]))
            grouped.setdefault(key, [unit, []])[1].append(record)

        changed = False
        for (section_name, _), (unit, unit_records) in sorted(
            grouped.items(),
            key=lambda item: (item[0][0], item[0][1]),
            reverse=True,
        ):
            lines = sections[section_name]
            record = unit_records[0]
            ref = next(
                (
                    candidate
                    for candidate in (
                        _record_resource_ref(item, profile)
                        for item in unit_records
                    )
                    if candidate
                ),
                "",
            )
            changed |= self._patch_glow_unit(
                lines,
                unit,
                record.get("marker"),
                ref,
            )
        return changed

    def _target_draw_units(self, sections, targets, native_targets=None):
        jobs = []
        seen = set()
        for obj in targets:
            hash_val = _extract_hash_from_object(obj.name).lower()
            if not hash_val:
                print(f"[GlowFX] 跳过 {obj.name}: 无法解析哈希")
                continue
            suffix_pattern = _normalize_suffix(_extract_resource_suffix(obj.name))
            matches = self._collect_target_units(
                sections,
                obj,
                hash_val,
                suffix_pattern,
            )
            if native_targets is not None and not matches:
                native_targets.append(obj)
            for section_name, units in matches:
                for unit in units:
                    key = (section_name, int(unit["start"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    jobs.append((section_name, unit))

        return sorted(
            jobs,
            key=lambda item: (item[0], int(item[1]["start"])),
            reverse=True,
        )

    def _apply_native_surface_flow(self, sections, targets, ini_file):
        from ..common.zzmi_surface_flow import surface_draw_scope, surface_shader_sections

        native_targets = []
        jobs = self._target_draw_units(sections, targets, native_targets)
        native_draws = {}
        for obj in native_targets:
            identity = ini_scope.mesh_ib_key(obj.name)
            if not identity or not identity["full"]:
                print(f"[GlowFX] 跳过 {obj.name}: 原生绘制需要完整的 IB 哈希、索引数和起始索引")
                continue
            hash_val, index_count, first_index = identity["full"].split("-")
            native_draws[(hash_val, int(index_count), int(first_index))] = obj.name
        if not jobs and not native_draws:
            return False
        brightness = self._brightness_slider_value(sections, ini_file)
        mode = getattr(self, "native_surface_mode", "MIRROR")
        replay = getattr(self, "native_surface_replay", False)
        height_scale = 0.0
        if mode == 'HEIGHT':
            heights = []
            for obj in targets:
                if obj.type == 'MESH':
                    x, y, z, offset = obj.matrix_world[2]
                    heights.extend(x * a + y * b + z * c + offset for a, b, c in obj.bound_box)
            if not heights or max(heights) - min(heights) <= 1e-6:
                raise ValueError("高光流光需要具有高度的目标网格")
            height_scale = 1.0 / (max(heights) - min(heights))
        tag = self._var_tag()
        switch = self._effect_switch_variable()
        texture = None
        if not replay and getattr(self, "native_surface_use_texture", False):
            texture = f"ResourceZZMISurfaceFlowTexture{tag}"
            selected = self.native_surface_texture
            source = (
                Path(bpy.path.abspath(selected, library=getattr(self.id_data, "library", None)))
                if selected else Path(__file__).resolve().parents[1] / "resources" / "zzmi_surface_flow.dds"
            )
            if not source.is_file():
                raise ValueError(f"流光贴图不存在：{source}")
            contents = source.read_bytes()
            if len(contents) < 128 or contents[:4] != b"DDS ":
                raise ValueError(f"流光贴图必须是 DDS：{source}")
            relative = Path("Textures") / f"ZZMISurfaceFlow-{hashlib.sha256(contents).hexdigest()[:16]}.dds"
            output = Path(ini_file).parent / relative
            output.parent.mkdir(exist_ok=True)
            if not output.is_file():
                output.write_bytes(contents)
            sections[f"[{texture}]"] = [f"filename = {relative.as_posix()}"]
            sections[f"[ResourceZZMISurfaceFlowSaved{tag}]"] = []
        changed = False
        for section_name, unit in jobs:
            lines = sections[section_name]
            start, end = _unit_edit_scope(lines, unit)
            block = _strip_previous_glowfx_blocks(_strip_previous_blocks(lines[start:end]))
            block = _strip_material_rabbitfx_lines(_strip_rabbitfx_glow_lines(block, self.fx_profile))
            draws = [index for index, line in enumerate(block) if _draw_arguments(line) is not None]
            if not draws:
                continue
            for index in reversed(draws):
                indent = _leading_whitespace(block[index])
                setup, restore = surface_draw_scope(
                    tag, f"{start}_{index}",
                    self.native_surface_speed, brightness,
                    self.native_surface_color,
                    height_scale, texture, switch,
                )
                if replay:
                    args = _draw_arguments(block[index])
                    replay_lines = [
                        f"$surface_replay_{param} = {value}"
                        for param, value in zip(("count", "start", "base"), args)
                    ]
                    replay_lines.append(f"run = CommandListZZMISurfaceReplayIndexed{tag}")
                    _insert_block(block, index + 1, [_CALL_BEGIN, *replay_lines, _CALL_END], indent)
                    continue
                # All draw-specific state is assigned inline while the original PS is bound.
                _insert_block(block, index + 1, [_CALL_BEGIN, *restore, _CALL_END], indent)
                _insert_block(block, index, [_PARAM_BEGIN, *setup, _PARAM_END], indent)
            lines[start:end] = block
            changed = True
        for (hash_val, index_count, first_index), object_name in native_draws.items():
            draw_id = f"{hash_val}_{index_count}_{first_index}"
            setup, restore = surface_draw_scope(
                tag, draw_id,
                self.native_surface_speed, brightness,
                self.native_surface_color, height_scale, texture, switch,
            )
            commands = (
                [f"post run = CommandListZZMISurfaceReplayNative{tag}"]
                if replay else [*setup, *(line.replace("pre ", "post ", 1) for line in restore)]
            )
            sections[f"[TextureOverrideZZMISurfaceFlowNative_{draw_id}]"] = [
                f"; 原生绘制: {object_name}",
                f"hash = {hash_val}",
                f"match_index_count = {index_count}",
                f"match_first_index = {first_index}",
                "allow_duplicate_hash = true",
                *commands,
            ]
            changed = True
        if changed and replay:
            self._emit_surface_replay(sections, tag, ini_file, bool(jobs), bool(native_draws), height_scale, brightness)
        elif changed:
            textured = any(name.startswith("[ResourceZZMISurfaceFlowTexture") for name in sections)
            sections.update(surface_shader_sections(textured))
        return changed

    def _emit_surface_replay(self, sections, tag, ini_file, indexed, native, height_scale, brightness):
        from ..common.zzmi_surface_flow import surface_draw_scope

        relative = "res/surface_flow_replay_ps.hlsl"
        source = Path(__file__).resolve().parents[1] / "Toolset" / "glow_fx" / relative
        output = Path(ini_file).parent / relative
        output.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, output)
        layouts = (
            ("Packed", 73412, "xyzw", r"mov o2\.w,[^\r\n]*\r?\n[ \t]*mov o2\.xyz"),
            ("Face", 73413, "xyz", r"mov o4\.xyz,[^\r\n]*"),
        )
        for layout, filter_index, mask, assignment in layouts:
            classifier = f"ShaderRegexZZMISurfaceReplay{layout}Layout"
            sections[f"[{classifier}]"] = ["shader_model = vs_5_0", f"filter_index = {filter_index}"]
            sections[f"[{classifier}.Pattern]"] = [
                rf"(?ms)^dcl_output o2\.{mask}\r?\n[ \t]*dcl_output o3\.{mask}\r?\n"
                rf"[ \t]*dcl_output o4\.{mask}\r?\n.*?^dcl_output_siv o9\.xyzw, position\r?\n.*?^[ \t]*{assignment}"
            ]
            wrapper = output.with_name(f"surface_flow_replay_{layout.lower()}_ps.hlsl")
            wrapper.write_text(f'#define REPLAY_{layout.upper()}\n#include "{output.name}"\n', encoding="utf-8")
        for resource in ("NormalTarget", "SavedTexture", "SavedCamera"):
            sections.setdefault(f"[ResourceZZMISurfaceReplay{resource}]", [])
        if indexed:
            constants = sections.setdefault("[Constants]", [])
            for param in ("count", "start", "base"):
                line = f"global $surface_replay_{param} = 0"
                if line not in constants:
                    constants.append(line)
        setup, restore = surface_draw_scope(
            tag, "replay", self.native_surface_speed, brightness,
            self.native_surface_color, height_scale,
        )
        switch = self._effect_switch_variable()
        for name, enabled, draw in (
            ("Indexed", indexed, "drawindexed = $surface_replay_count, $surface_replay_start, $surface_replay_base"),
            ("Native", native, "draw = from_caller"),
        ):
            if not enabled:
                continue
            shader = f"CustomShaderZZMISurfaceReplay{name}"
            sections[f"[{shader}]"] = [
                f"ps = {relative}",
                "depth_enable = true", "depth_write_mask = zero", "depth_func = equal",
                "depth_stencil_state_merge = true", "stencil_write_mask = 0",
                "blend[0] = add one one", "mask[0] = 7",
                *(f"mask[{slot}] = 0" for slot in range(1, 8)),
                draw,
            ]
            for layout, _, _, _ in layouts:
                shader_lines = list(sections[f"[{shader}]"])
                shader_lines[0] = f"ps = res/surface_flow_replay_{layout.lower()}_ps.hlsl"
                sections[f"[{shader}{layout}]"] = shader_lines
            sections[f"[CommandListZZMISurfaceReplay{name}{tag}]"] = [
                r"if @od && @o0 && @o3 && !@o4 && !$\slotfix\is_outline_shader"
                + (f" && {switch} != 0" if switch else ""),
                "    local $saved_width = x495",
                "    local $saved_height = y495",
                "    x495 = rt_width",
                "    y495 = rt_height",
                *(f"    {line.removeprefix('pre ')}" for line in setup),
                "    ResourceZZMISurfaceReplayNormalTarget = ref o3",
                "    ResourceZZMISurfaceReplaySavedTexture = ref ps-t118",
                "    ResourceZZMISurfaceReplaySavedCamera = ref ps-cb13",
                "    o3 = null",
                "    ps-t118 = ref ResourceZZMISurfaceReplayNormalTarget",
                "    ps-cb13 = ref vs-cb0",
                f"    if vs == {layouts[0][1]}",
                f"        run = {shader}Packed",
                f"    else if vs == {layouts[1][1]}",
                f"        run = {shader}Face",
                "    else",
                f"        run = {shader}",
                "    endif",
                "    ps-t118 = ref ResourceZZMISurfaceReplaySavedTexture",
                "    ps-cb13 = ref ResourceZZMISurfaceReplaySavedCamera",
                "    o3 = ref ResourceZZMISurfaceReplayNormalTarget",
                "    ResourceZZMISurfaceReplayNormalTarget = null",
                "    ResourceZZMISurfaceReplaySavedTexture = null",
                "    ResourceZZMISurfaceReplaySavedCamera = null",
                *(f"    {line.removeprefix('pre ')}" for line in restore),
                "    x495 = $saved_width",
                "    y495 = $saved_height",
                "endif",
            ]

    def _apply_fallback_injections(self, sections, targets):
        changed = False
        for section_name, unit in self._target_draw_units(sections, targets):
            lines = sections[section_name]
            changed |= self._patch_glow_unit(
                lines,
                unit,
                None,
                "",
            )
        return changed, changed

    def _patch_glow_unit(
        self,
        lines,
        unit,
        marker,
        record_ref,
    ):
        start, end = _unit_edit_scope(lines, unit)
        block = _strip_previous_blocks(lines[start:end])
        ref = record_ref or _block_resource_ref(block, self.fx_profile)
        if not ref:
            for line in block:
                stripped = line.strip()
                match = (_LEGACY_GLOW_REF_RE.match(stripped) or _LOCAL_GLOW_REF_RE.match(stripped)
                         or _EFMI_GLOW_REF_RE.match(stripped))
                if match:
                    ref = match.group(1)
                    break
        block = _strip_previous_glowfx_blocks(block)
        if not ref:
            print(f"[GlowFX] 跳过 {unit.get('mesh', '')}: 找不到 Glowmap 资源引用")
            return False
        # 只在本目标单元内清 RabbitFX：同蓝图另有 RabbitFXPro 目标时，别动它的单元。
        block = _strip_rabbitfx_glow_lines(block, self.fx_profile)
        block = _strip_material_rabbitfx_lines(block)

        draws = list(_glow_draws(block, efmi=GlobalConfig.logic_name == LogicName.EFMI))
        if not draws:
            print(f"[GlowFX] 跳过 {unit.get('mesh', '')}: 找不到绘制参数")
            return False

        for start_index, draw_index, draw_args, draw_kind in reversed(draws):
            if draw_kind == "EFMITTL":
                self._insert_efmi_glow_draw(block, start_index, draw_index, ref)
            elif draw_kind == "EFMI":
                self._insert_efmi_glow_replay(block, draw_index, draw_args, ref)
            else:
                self._insert_glow_draw(block, start_index, draw_index, draw_args, ref, draw_kind == "TTL")
        lines[start:end] = block
        return True

    def _insert_glow_draw(self, block, start_index, draw_index, draw_args, ref, ttl):
        indent = _leading_whitespace(block[draw_index])
        emission_only = self.emission_only
        switch = self._effect_switch_variable()
        # 只有「仅绘制发光贴图」才需要临时收起原始 draw；默认导出不包任何 guard，
        # 避免把 SlotFix 之类的运行时变量塞在原始 drawindexed 外面（未声明即风险）。
        call_lines = [
            _CALL_BEGIN,
        ]
        if emission_only:
            call_lines.append("endif")
            if ttl:
                call_lines.extend([
                    "if $glowfx_emission_only",
                    r"run = CommandList\TTL\ResetCallParams",
                    r"Resource\TTL\TransparencyTex = null",
                    "endif",
                ])
        if switch:
            call_lines.append(f"if {switch} != 0")
        suffix = self._halo_suffix()
        if not ttl:
            call_lines.append(f"{GLOW_RESOURCE}{suffix} = ref {ref}")
        call_lines.extend([
            f"$glowfx_drawcount = {draw_args[0]}",
            f"$glowfx_drawstart = {draw_args[1]}",
            f"$glowfx_drawbase = {draw_args[2]}",
            RUN_LINE + suffix,
        ])
        if self.enable_reset:
            call_lines.extend(
                [
                    _RESET_BEGIN,
                    "$glowfx_h = 0",
                    "$glowfx_s = 0",
                    "$glowfx_v = 0",
                    "$glowfx_interpolate = 0",
                    _RESET_END,
                ]
            )
        if switch:
            call_lines.append("endif")
        call_lines.append(_CALL_END)
        _insert_block(block, draw_index + 1, call_lines, indent)

        param_lines = [_PARAM_BEGIN]
        if self.enable_glow:
            param_lines.extend(self._build_glow_parameter_lines())
        if emission_only:
            param_lines.append(
                f"$glowfx_emission_only = ({switch} != 0)" if switch else "$glowfx_emission_only = 1"
            )
        if ttl:
            if switch:
                param_lines.append(f"if {switch} != 0")
            param_lines.extend([
                f"{GLOW_RESOURCE}{suffix} = ref {ref}",
                f"run = CommandListGeoGlowPrepareTTL{suffix}",
            ])
            if switch:
                param_lines.append("endif")
        if emission_only:
            param_lines.append("if !$glowfx_emission_only")
        param_lines.append(_PARAM_END)
        _insert_block(block, start_index, param_lines, indent)

    def _insert_efmi_glow_replay(self, block, draw_index, draw_args, ref):
        indent = _leading_whitespace(block[draw_index])
        switch = self._effect_switch_variable()
        call = [_CALL_BEGIN]
        if self.emission_only:
            call.append("endif")
        if switch:
            call.append(f"if {switch} != 0")
        if self.enable_glow:
            call.extend(self._build_glow_parameter_lines())
        call.append(f"{GLOW_RESOURCE} = ref {ref}")
        call.extend(f"$efmiglowfx_{name} = {value}" for name, value in zip(_EFMI_DRAW_PARAMS, draw_args))
        call.append(_EFMI_GLOW_RUN)
        if switch:
            call.append("endif")
        call.append(_CALL_END)
        _insert_block(block, draw_index + 1, call, indent)
        if self.emission_only:
            keep_original = f"if !({_EFMI_GLOW_PASS})"
            if switch:
                keep_original += f" || {switch} == 0"
            _insert_block(block, draw_index, [_PARAM_BEGIN, keep_original, _PARAM_END], indent)

    def _insert_efmi_glow_draw(self, block, start_index, draw_index, ref):
        indent = _leading_whitespace(block[draw_index])
        switch = self._effect_switch_variable()
        tag = self._var_tag()
        self._glowfx_efmi_used = True
        if self.enable_glow:
            only_value = _glowfx_param("only", tag)
            radius_value = _glowfx_param("radius", tag)
            normalization_value = _glowfx_param("normalization", tag)
        else:
            # 关闭发光时保持旧导出逐字节不变。
            only_value = int(self.emission_only)
            radius_value = self.halo_radius
            normalization_value = f"{_halo_normalization(self.halo_radius):.8f}"
        setup = [_PARAM_BEGIN]
        if self.enable_glow:
            setup.extend(self._build_glow_parameter_lines())
        if switch:
            setup.append(f"if {switch} != 0")
        setup.extend([
            rf"Resource\EFMITTL\GlowFXMap = ref {ref}",
            r"$\EFMITTL\glowfx_hue = ($glowfx_h / 360.0) * $glowfx_interpolate",
            r"$\EFMITTL\glowfx_saturation = ($glowfx_s / 100.0) * $glowfx_interpolate",
            r"$\EFMITTL\glowfx_value = ($glowfx_v / 100.0) * $glowfx_interpolate",
            r"$\EFMITTL\glowfx_brightness = $glowfx_brightness",
            rf"$\EFMITTL\glowfx_only = {only_value}",
            rf"$\EFMITTL\glowfx_radius = {radius_value}",
            rf"$\EFMITTL\glowfx_normalization = {normalization_value}",
        ])
        if switch:
            setup.append("endif")
        setup.append(_PARAM_END)
        _insert_block(block, draw_index + 1, [
            _CALL_BEGIN, "$glowfx_h = 0", "$glowfx_s = 0", "$glowfx_v = 0",
            "$glowfx_brightness = 1", "$glowfx_interpolate = 0", _CALL_END,
        ], indent)
        _insert_block(block, start_index, setup, indent)

    def _build_glow_parameter_lines(self):
        if self._breathing():
            _, values = self._split_breath_lines()
            return [_BREATH_BEGIN, *values, _BREATH_END]
        tag = self._var_tag()
        return [
            f"{_glowfx_param(name)} = "
            + (self._brightness_slider_value() if name == "brightness" and self.brightness_slider_enabled()
               else _glowfx_param(name, tag))
            for name in ("h", "s", "v", "brightness", "interpolate")
        ]

    def _build_breath_lines(self, breath_tick_count=1):
        brightness_max = self._brightness_slider_value() if self.brightness_slider_enabled() else self.breath_brightness_max
        lines = build_breath_parameter_lines(
            _glowfx_param,
            var_prefix=_VAR_PREFIX,
            breath_begin=_BREATH_BEGIN,
            breath_end=_BREATH_END,
            tag=self._var_tag(),
            fps=self.breath_fps,
            step=self.breath_step,
            mode=self.breath_mode,
            h=self.breath_h,
            s=self.breath_s,
            v=self.breath_v,
            brightness_min=self.breath_brightness_min,
            brightness_max=brightness_max,
            interpolate=self.breath_interpolate,
            tick_count=breath_tick_count,
            smooth_combo=True,
        )
        if isinstance(brightness_max, str) and self.breath_mode != 'RAINBOW':
            lines[-1:-1] = [
                f"if ({brightness_max}) < {_format_float(self.breath_brightness_min)}",
                f"    {_glowfx_param('brightness')} = {brightness_max}",
                "endif",
            ]
        return lines

    def _split_breath_lines(self):
        lines = self._build_breath_lines()
        hue_assignment = f"{_glowfx_param('h')} = "
        first_value = next(i for i, line in enumerate(lines) if line.startswith(hue_assignment))
        return lines[1:first_value], lines[first_value:-1]

    def _ensure_breath_clock(self, sections):
        clock, _ = self._split_breath_lines()
        tag = self._var_tag()
        begin = f"; === GlowFX Breath Clock {tag} ==="
        end = f"; === End GlowFX Breath Clock {tag} ==="
        present = sections.setdefault("[Present]", [])
        if begin in present and end in present:
            del present[present.index(begin):present.index(end) + 1]
        present.extend([begin, *clock, end])

    def _ensure_glow_parameter_constants(self, sections):
        """每个节点实例的每组面板参数在 [Constants] 恰好声明一次。

        同 tag 的旧声明原地更新（改面板值重导只动声明行），不追加第二份。
        呼吸灯模式下的 h/s/v/brightness/interpolate 是逐帧算出来的，不声明。
        """
        if not self.enable_glow:
            return False
        tag = self._var_tag()
        wanted = []
        if not self._breathing():
            values = (
                ("h", _format_float(self.glow_h)),
                ("s", _format_float(self.glow_s)),
                ("v", _format_float(self.glow_v)),
                ("brightness", _format_float(self.glow_brightness)),
                ("interpolate", _format_float(self.glow_interpolate)),
            )
            wanted.extend(
                f"global {_glowfx_param(name, tag)} = {value}"
                for name, value in values
            )
        if getattr(self, "_glowfx_efmi_used", False):
            wanted.extend([
                f"global {_glowfx_param('only', tag)} = {int(self.emission_only)}",
                f"global {_glowfx_param('radius', tag)} = {self.halo_radius}",
                f"global {_glowfx_param('normalization', tag)} = "
                f"{_halo_normalization(self.halo_radius):.8f}",
            ])
        if not wanted:
            return False
        constants = sections.setdefault("[Constants]", [])
        safe_tag = re.sub(r"[^a-z0-9_]", "_", tag.lower()).strip("_") or "0"
        own_re = re.compile(
            rf"^\s*global\s+(?:persist\s+)?\$glowfx_{re.escape(safe_tag)}_"
            rf"(?:{'|'.join(_GLOW_PARAM_NAMES)})\s*=",
            re.IGNORECASE,
        )
        changed = False
        for line in wanted:
            variable = line.split("=", 1)[0].split()[-1]
            pattern = re.compile(
                rf"^\s*global\s+(?:persist\s+)?{re.escape(variable)}\s*=",
                re.IGNORECASE,
            )
            indexes = [
                index for index, existing in enumerate(constants)
                if pattern.match(str(existing))
            ]
            if indexes:
                first = indexes[0]
                if str(constants[first]).strip() != line:
                    constants[first] = line
                    changed = True
                for index in reversed(indexes[1:]):
                    del constants[index]
                    changed = True
                continue
            own = [
                index for index, existing in enumerate(constants)
                if own_re.match(str(existing))
            ]
            if own:
                position = own[-1] + 1
            else:
                position = next(
                    (
                        index for index, existing in enumerate(constants)
                        if _GLOW_GLOBAL_RE.match(str(existing))
                    ),
                    len(constants),
                )
            constants.insert(position, line)
            changed = True
        return changed

    def _ensure_glow_constants(self, sections):
        tag = self._var_tag()
        wanted = [
            f"global persist {_VAR_PREFIX}{tag}_frame = 0",
            f"global {_VAR_PREFIX}{tag}_valve = 0",
            f"global {_VAR_PREFIX}{tag}_glow = 0",
        ]
        constants = sections.setdefault("[Constants]", [])
        existing = {str(line).strip() for line in constants}
        added = [line for line in wanted if line not in existing]
        if not added:
            return False
        constants.extend(added)
        return True

    @staticmethod
    def _resource_source_root():
        return os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "Toolset",
                "glow_fx",
            )
        )

    def _embed_glow_runtime(self, sections):
        template = os.path.join(self._resource_source_root(), "GlowFX", "GlowFX.ini")
        with open(template, encoding="utf-8") as handle:
            source = handle.read()
        radius = self.halo_radius
        normalization = _halo_normalization(radius)
        source = source.replace("__GLOW_RADIUS__", str(radius))
        source = source.replace("__GLOW_NORMALIZATION__", f"{normalization:.8f}")
        suffix = self._halo_suffix()
        if suffix:
            source = _RUNTIME_NAME_RE.sub(lambda match: match.group() + suffix, source)
        runtime, _ = self._parse_ini(source)
        ttl_setup = f"run = CommandListGeoGlowPrepareTTL{suffix}".casefold()
        if not any(line.strip().casefold() == ttl_setup for lines in sections.values() for line in lines):
            runtime.pop(f"[CommandListGeoGlowPrepareTTL{suffix}]")
        constants = sections.setdefault("[Constants]", [])
        existing = {line.strip().casefold() for line in constants}
        constants.extend(line for line in runtime.pop("[Constants]") if line.casefold() not in existing)
        present = sections.setdefault("[Present]", [])
        existing_present = {line.strip().casefold() for line in present}
        present.extend(
            line for line in runtime.pop("[Present]")
            if line.strip().casefold() not in existing_present
        )
        sections.update(runtime)

    def _embed_efmi_glow_runtime(self, sections, mod_export_path):
        source_root = Path(self._resource_source_root())
        template = source_root / "EFMI" / "GlowFX.ini"
        source = template.read_text(encoding="utf-8").replace("__GLOW_PASS__", _EFMI_GLOW_PASS)
        runtime, _ = self._parse_ini(source)
        constants = sections.setdefault("[Constants]", [])
        existing = {line.split("=", 1)[0].strip().casefold() for line in constants}
        constants.extend(
            line for line in runtime.pop("[Constants]")
            if line.split("=", 1)[0].strip().casefold() not in existing
        )
        sections.update(runtime)
        output = Path(mod_export_path) / "res"
        output.mkdir(exist_ok=True)
        shutil.copy2(source_root / "res" / "efmi_glow_fx_ps.hlsl", output)

    def _copy_glow_fx_resources(self, mod_export_path):
        source_dir = os.path.join(self._resource_source_root(), "res")
        target_dir = os.path.join(mod_export_path, "res")
        os.makedirs(target_dir, exist_ok=True)
        for filename in (
            "glow_fx_ps.hlsl",
            "glow_fx_emission.hlsli",
            "geom_glow_vs.hlsl",
            "geom_glow_visibility_ps.hlsl",
            "geom_glow_layers.hlsli",
            "geom_glow_prefilter_ps.hlsl",
            "geom_glow_horizontal_ps.hlsl",
            "geom_glow_composite_ps.hlsl",
        ):
            shutil.copy2(os.path.join(source_dir, filename), target_dir)

    @staticmethod
    def _disable_legacy_glow_fx_ini(mod_export_path):
        legacy_ini = os.path.join(mod_export_path, "GlowFX", "GlowFX.ini")
        if not os.path.isfile(legacy_ini):
            return
        # A previously exported INI can coexist in the folder after a name change.
        for ini_file in glob.glob(os.path.join(mod_export_path, "*.ini")):
            with open(ini_file, encoding="utf-8-sig") as handle:
                if any(line.strip().casefold() == _LEGACY_RUN_LINE.casefold() for line in handle):
                    return
        with open(legacy_ini, encoding="utf-8-sig") as handle:
            content = handle.read().casefold()
        if "namespace = glowfx" in content and "[customshaderglowfx]" in content:
            os.rename(legacy_ini, legacy_ini + ".disabled")


_classes = (SSMTNode_PostProcess_GlowFX,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
