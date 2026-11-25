"""Apply per-object parameters to the native EFMI TTL draws."""

import re
import uuid
from pathlib import Path

import bpy

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.ttl_protocol import parse_efmi_ttl_draw_call
from . import ini_scope
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_rabbitfx_pro import (
    SSMTNode_PostProcess_RabbitFXPro,
    TTL_NODE_IDNAME,
    _GLOW_CONTROL_NAMES,
    _breath_control_defaults,
    _format_float,
    _glow_binding_brightness_indices,
    _glow_control_defaults,
    _ib_key_same_hash,
    _node_control_label,
    _objects_used_by_other_nodes,
    _on_preview_parameter_update,
    _part_name,
    _probe_marked_slots,
    _PROBE,
    _referenced_controls,
    _unit_matches_target,
    build_breath_parameter_lines,
)


_VAR_PREFIX = "$ttlglow_"
_TTL_RUN_RE = re.compile(
    r"^\s*run\s*=\s*CommandList\\EFMITTL\\(Draw|SetGlow)\s*(?:;.*)?$",
    re.IGNORECASE,
)
_TTL_REF_RE = re.compile(
    r"^\s*Resource\\EFMITTL\\(GlowMap|Mask)\s*=\s*ref\s+([^\s;]+)",
    re.IGNORECASE,
)
_CLOCK_MARKER_RE = re.compile(r"^; === TTL Glow (\w+) (End )?(?:Constants|Breath) ===$")


def _param(name):
    return rf"$\EFMITTL\{name}"


def _apply_settings(section_name, lines, targets, glow_values, mask_values, tag, newline):
    """Insert settings after the material switches and before the TTL draw arguments."""
    units = list(ini_scope.iter_draw_units(lines))
    section_scope = ini_scope.section_ib_key(section_name, lines) if units else None
    edits = {}
    remove = set()
    claimed = set()
    for unit in reversed(units):
        mesh = unit["mesh"]
        mesh_name = mesh.strip().casefold()
        mesh_scope = ini_scope.mesh_ib_key(mesh) or section_scope
        is_target = any(
            _ib_key_same_hash(scope, mesh_scope)
            and (mesh_name == name or _unit_matches_target(mesh, scope, part))
            for name, scope, part in targets
        )
        mask_bound = False
        glow_bindings = []
        for index in range(unit["start"] + 1, unit["end"]):
            line = lines[index]
            binding = _TTL_REF_RE.match(line)
            if binding and binding.group(2).casefold() != "null":
                if binding.group(1).casefold() == "glowmap":
                    glow_bindings.append(index)
                else:
                    mask_bound = True
            run = _TTL_RUN_RE.fullmatch(line.strip())
            if not run:
                continue
            is_draw = run.group(1).casefold() == "draw"
            if is_target and index not in claimed:
                values = list(glow_values) if glow_bindings else []
                if values:
                    for binding_index in glow_bindings:
                        remove.update(_glow_binding_brightness_indices(
                            lines, binding_index, _param("brightness"), unit["start"] + 1, index,
                        ))
                insert_at = index
                if is_draw:
                    draw = parse_efmi_ttl_draw_call(lines, index, unit["start"] + 1)
                    if draw:
                        insert_at = draw[0]
                    if mask_bound:
                        values.extend(mask_values)
                if values:
                    indent = re.match(r"[ \t]*", lines[insert_at])[0]
                    edits[insert_at] = _marked_block(tag, "Params", values, newline, indent)
            claimed.add(index)
            glow_bindings.clear()
            if is_draw:
                mask_bound = False
    for index in sorted(edits.keys() | remove, reverse=True):
        lines[index:index + (index in remove)] = edits.get(index, ())
    return bool(edits)


def _marker(tag, kind):
    return f"; === TTL Glow {tag} {kind} ==="


def _marked_block(tag, kind, lines, newline, indent=""):
    return [
        indent + line + newline
        for line in (_marker(tag, kind), *lines, _marker(tag, f"End {kind}"))
    ]


def _strip_generated_blocks(lines, tag):
    start_markers = {_marker(tag, kind): _marker(tag, f"End {kind}")
                     for kind in ("Params", "Constants", "Breath")}
    cleaned = []
    index = 0
    while index < len(lines):
        end = start_markers.get(lines[index].strip())
        if end:
            end_index = next(
                (i for i in range(index + 1, len(lines)) if lines[i].strip() == end),
                None,
            )
            if end_index is not None:
                index = end_index + 1
                continue
        cleaned.append(lines[index])
        index += 1
    return cleaned


def _add_to_section(lines, section_name, block, tag, newline):
    for name, _, start, end in ini_scope.find_sections(lines):
        if name.casefold() == section_name.casefold():
            position = start
            for index in range(start, end):
                marker = _CLOCK_MARKER_RE.fullmatch(lines[index].strip())
                if marker and marker.group(1) > tag:
                    break
                if marker and marker.group(2):
                    position = index + 1
            if not lines[position - 1].endswith(("\r", "\n")):
                lines[position - 1] += newline
            lines[position:position] = block
            return
    if lines and not lines[-1].endswith(("\r", "\n")):
        lines[-1] += newline
    lines.extend([f"[{section_name}]{newline}", *block])


class SSMTNode_PostProcess_TTL(SSMTNode_PostProcess_Base):
    bl_idname = TTL_NODE_IDNAME
    bl_label = "TTL 贴图后处理"
    bl_description = "调整 EFMI 原生 TTL 的发光、呼吸灯与半透明遮罩"

    __annotations__ = {
        name: prop for name, prop in SSMTNode_PostProcess_RabbitFXPro.__annotations__.items()
        if name.startswith(("glow_", "breath_", "preview_"))
        or name in {
            "target_items", "active_target_index", "buffer_tag", "enable_breath",
            "show_targets", "show_glow", "show_breath",
        }
    }

    enable_glow: bpy.props.BoolProperty(
        name="启用发光调整", default=True, update=_on_preview_parameter_update,
    )
    glow_brightness: bpy.props.FloatProperty(
        name="发光强度",
        description="EFMITTL 的 HDR 发光强度，0 关闭发光",
        default=8.0, min=0, max=500, update=_on_preview_parameter_update,
    )
    glow_interpolate: bpy.props.FloatProperty(
        name="插值", description="0 保留贴图原色，1 应用完整 HSV 偏移",
        default=1.0, min=0, max=1, update=_on_preview_parameter_update,
    )
    glow_mode: bpy.props.EnumProperty(
        name="发光混合模式",
        items=[
            ('MIX', "按发光贴图 Alpha 混合", "按发光贴图的 Alpha 保留底色", 0),
            ('ADD', "叠加发光", "在底色上加上发光颜色", 1),
            ('REPLACE', "替换底色", "在有发光的区域用发光颜色替换底色", 2),
        ],
        default='MIX',
    )
    show_ttl: bpy.props.BoolProperty(default=False)
    override_ttl_mask: bpy.props.BoolProperty(name="覆盖遮罩取样", default=False)
    ttl_mask_channel: bpy.props.EnumProperty(
        name="遮罩通道",
        items=[('R', "R", "", 0), ('G', "G", "", 1), ('B', "B", "", 2), ('A', "A", "", 3)],
        default='A',
    )
    ttl_mask_invert: bpy.props.BoolProperty(name="反转遮罩", default=False)
    override_ttl_alpha: bpy.props.BoolProperty(
        name="覆盖整体不透明度",
        description="覆盖 TTLMap 材质名指定的不透明度，继续乘以遮罩取样值",
        default=False,
    )
    ttl_alpha: bpy.props.FloatProperty(name="不透明度", default=1.0, min=0, max=1)

    _breathing = SSMTNode_PostProcess_RabbitFXPro._breathing
    _draw_breath_block = SSMTNode_PostProcess_RabbitFXPro._draw_breath_block
    _draw_glow_block = SSMTNode_PostProcess_RabbitFXPro._draw_glow_block
    _draw_preview = SSMTNode_PostProcess_RabbitFXPro._draw_preview
    _draw_targets = SSMTNode_PostProcess_RabbitFXPro._draw_targets
    _var_tag = SSMTNode_PostProcess_RabbitFXPro._var_tag
    active_target_object = SSMTNode_PostProcess_RabbitFXPro.active_target_object

    def init(self, context):
        super().init(context)
        self.buffer_tag = uuid.uuid4().hex[:8]
        self.width = 380

    def copy(self, source):
        self.buffer_tag = uuid.uuid4().hex[:8]

    def draw_buttons(self, context, layout):
        from ..common.global_properties import GlobalProterties

        if GlobalConfig.logic_name != LogicName.EFMI:
            layout.label(text="适用于 EFMI 游戏类型", icon='INFO')
        elif self.enable_glow:
            use_ttl_slot = getattr(GlobalProterties, "use_efmittl_slot", None)
            if use_ttl_slot is not None:
                if not use_ttl_slot():
                    layout.label(text="发光需要在输出节点启用「使用TTL槽位风格」", icon='INFO')
            elif GlobalProterties.use_rabbitfx_slot():
                layout.label(text="TTL 发光需要关闭「使用RabbitFX槽位风格」", icon='INFO')
        self._draw_targets(layout)
        self._draw_glow_block(layout)
        self._draw_ttl_block(layout)
        self._draw_preview(layout)

    def _draw_ttl_block(self, layout):
        row = layout.row(align=True)
        row.prop(self, "show_ttl", text="", emboss=False,
                 icon='TRIA_DOWN' if self.show_ttl else 'TRIA_RIGHT')
        row.label(text="TTL 贴图参数", icon='TEXTURE')
        if not self.show_ttl:
            return
        box = layout.box()
        row = box.row()
        row.enabled = self.enable_glow
        row.prop(self, "glow_mode")
        box.prop(self, "override_ttl_mask")
        mask = box.column(align=True)
        mask.enabled = self.override_ttl_mask
        mask.prop(self, "ttl_mask_channel", expand=True)
        mask.prop(self, "ttl_mask_invert")
        box.prop(self, "override_ttl_alpha")
        alpha = box.row()
        alpha.enabled = self.override_ttl_alpha
        alpha.prop(self, "ttl_alpha", slider=True)

    @staticmethod
    def _probe_target_textures(obj):
        has_glow = bool(_PROBE.find_matching_materials(obj, "Glowmap")) or _probe_marked_slots(obj)[0]
        return has_glow, bool(_PROBE.find_matching_materials(obj, "TTLMap"))

    def validate_export_configuration(self):
        if GlobalConfig.logic_name != LogicName.EFMI:
            return
        conflicts = []
        for item in self.target_items:
            obj = item.target_object
            if obj is not None:
                owner = _objects_used_by_other_nodes(self, obj)
                if owner:
                    conflicts.append(f"{obj.name}（{owner}）")
        if conflicts:
            raise ValueError("TTL 贴图后处理重复设置了这些物体：" + "、".join(conflicts))

    def execute_postprocess(self, mod_export_path):
        if GlobalConfig.logic_name != LogicName.EFMI:
            return

        from ..common.safe_write import write_bytes_if_changed_atomic
        from .model import BluePrintModel

        names = {item.target_object.name for item in self.target_items if item.target_object}
        names.update(BluePrintModel.get_mapped_object_name(name) for name in tuple(names))
        targets = [(name.casefold(), ini_scope.mesh_ib_key(name), _part_name(name)) for name in names]
        for ini_file in sorted(Path(mod_export_path).glob("*.ini")):
            content = ini_file.read_bytes().decode("utf-8")
            updated = self._patch_ini(content, targets)
            if updated != content:
                self._create_cumulative_backup(ini_file, mod_export_path)
                write_bytes_if_changed_atomic(ini_file, updated.encode("utf-8"))

    def _patch_ini(self, content, targets):
        bom = "\ufeff" if content.startswith("\ufeff") else ""
        original = content[len(bom):]
        driver, body = self.split_anim_driver_block_content(original)
        body, tail = self.split_auto_appended_tail_content(body)
        newline = "\r\n" if "\r\n" in original else "\n"
        tag = self._var_tag()
        prefix = f"{_VAR_PREFIX}{tag}_"
        lines = _strip_generated_blocks(body.splitlines(keepends=True), tag)
        defaults = self._mask_control_defaults()
        mask_values = [f"{_param(name)} = {prefix}{name}" for name in defaults]
        glow_values = []
        if self.enable_glow:
            glow_values = [f"{_param(name)} = {prefix}{name}"
                           for name in (*_GLOW_CONTROL_NAMES, "glow_mode")]
            defaults["glow_mode"] = ("MIX", "ADD", "REPLACE").index(self.glow_mode)

        for name, _, start, end in reversed(ini_scope.find_sections(lines)):
            section = lines[start:end]
            if _apply_settings(name, section, targets, glow_values, mask_values, tag, newline):
                lines[start:end] = section
        used = _referenced_controls(prefix, lines)
        clock = []
        if "brightness" in used:
            if self._breathing():
                clock = self._breath_clock(tag, prefix)
                used.update(_referenced_controls(prefix, clock))
                defaults.update(_breath_control_defaults(self))
                defaults.update(dict.fromkeys(("frame", "valve", "glow", *_GLOW_CONTROL_NAMES), 0))
            else:
                defaults.update(_glow_control_defaults(self))
        constants = [
            f"global {prefix}{name} = {value}" for name, value in defaults.items() if name in used
        ]
        if constants:
            block = _marked_block(tag, "Constants", [f"; {_node_control_label(self)}", *constants], newline)
            _add_to_section(lines, "Constants", block, tag, newline)
        if clock:
            _add_to_section(lines, "Present", [line + newline for line in clock], tag, newline)
        updated = "".join(lines)
        if updated == body:
            return content
        if driver:
            updated = driver.rstrip("\r\n") + newline * 2 + updated
        return bom + updated + tail

    def _mask_control_defaults(self):
        values = {}
        if self.override_ttl_mask:
            values["mask_channel"] = "RGBA".index(self.ttl_mask_channel)
            values["mask_invert"] = int(self.ttl_mask_invert)
        if self.override_ttl_alpha:
            values["alpha"] = _format_float(self.ttl_alpha)
        return values

    def _breath_clock(self, tag, prefix):
        def variable(name):
            return f"{prefix}{name}"

        return build_breath_parameter_lines(
            variable,
            var_prefix=_VAR_PREFIX,
            breath_begin=_marker(tag, "Breath"),
            breath_end=_marker(tag, "End Breath"),
            tag=tag,
            fps=self.breath_fps,
            step=self.breath_step,
            mode=self.breath_mode,
            h=self.breath_h,
            s=self.breath_s,
            v=self.breath_v,
            brightness_min=self.breath_brightness_min,
            brightness_max=self.breath_brightness_max,
            interpolate=self.breath_interpolate,
            setting_ref=variable,
        )


def register():
    bpy.utils.register_class(SSMTNode_PostProcess_TTL)


def unregister():
    bpy.utils.unregister_class(SSMTNode_PostProcess_TTL)
