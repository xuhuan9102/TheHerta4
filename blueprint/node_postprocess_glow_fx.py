"""Glow FX着色器后处理节点。

该节点复用 RabbitFX Pro 的目标列表和面板属性，但导出时把目标的 RabbitFX
发光调用替换为随 Mod 分发的 ``GlowFX`` 专用着色器调用。
原始绘制、材质绑定和 ``ZZMI\\SetTextures`` 保持不变。
"""

import bpy
import glob
import os
import re
import shutil
import uuid
from collections import OrderedDict

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
    _section_fallback_unit,
    _strip_previous_blocks,
    _unit_edit_scope,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_rabbitfx import (
    _extract_hash_from_object,
    _extract_resource_suffix,
)

NODE_IDNAME = "SSMTNode_PostProcess_GlowFX"
RUN_LINE = "run = CommandListGlowFXRun"
_DRAW_GUARD = 'if !$\\SlotFix\\is_outline_shader'
GLOW_RESOURCE = "ResourceGlowFXMap"
_DEFAULT_HALO_RADIUS = 40
_LOCAL_GLOW_REF_RE = re.compile(r"^ResourceGlowFXMap(?:_r\d+)?\s*=\s*ref\s+(\S+)", re.IGNORECASE)
_RUNTIME_NAME_RE = re.compile(r"\b(?:\w*GeoGlow\w*|ResourceGlowFXMap|CommandListGlowFXRun)\b", re.IGNORECASE)
_LEGACY_RUN_LINE = r"run = CommandList\GlowFX\Run"
_LEGACY_GLOW_REF_RE = re.compile(r"^Resource\\GlowFX\\Glowmap\s*=\s*ref\s+(\S+)", re.IGNORECASE)


def _glowfx_param(name):
    return f"$glowfx_{name.lower()}"


_PARAM_BEGIN = "; === GlowFX Params ==="
_PARAM_END = "; === End GlowFX Params ==="
_BREATH_BEGIN = "; === GlowFX Breath ==="
_BREATH_END = "; === End GlowFX Breath ==="
_CALL_BEGIN = "; === GlowFX Call ==="
_CALL_END = "; === End GlowFX Call ==="
_RESET_BEGIN = "; === GlowFX Reset ==="
_RESET_END = "; === End GlowFX Reset ==="

_VAR_PREFIX = "$glowfx_"
_VAR_IF_RE = re.compile(r"^if\s+\$glowfx_", re.IGNORECASE)
_IF_LINE_RE = re.compile(r"^if\s+", re.IGNORECASE)
_DRAWINDEXED_RE = re.compile(
    r"^(?P<indent>\s*)drawindexed\s*=\s*(?P<args>[^;]+)",
    re.IGNORECASE,
)
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


def _first_draw_after(lines, start):
    for index in range(max(0, start), len(lines)):
        args = _draw_arguments(lines[index])
        if args is not None:
            return index, args
    return -1, None


class SSMTNode_PostProcess_GlowFX(
    _RabbitFXProPanelMixin,
    SSMTNode_PostProcess_Base,
):
    __annotations__ = dict(SSMTNode_PostProcess_RabbitFXPro.__annotations__)

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

    def _draw_breath_block(self, layout):
        layout.prop(self, "halo_radius")
        layout.prop(self, "emission_only")
        super()._draw_breath_block(layout)

    def _halo_suffix(self):
        return "" if self.halo_radius == _DEFAULT_HALO_RADIUS else f"_r{self.halo_radius}"

    def validate_export_configuration(self):
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
        with open(ini_file, "r", encoding="utf-8") as handle:
            content = handle.read()

        preserved_driver, content = self.split_anim_driver_block_content(content)
        content, tail = self.split_auto_appended_tail_content(content)
        sections, preamble = self._parse_ini(content)

        if records is None:
            changed, generated = self._apply_fallback_injections(sections, targets)
        else:
            changed = self._apply_material_records(sections, records, targets, "")
            generated = changed

        if not changed:
            return False
        if generated:
            self._embed_glow_runtime(sections)
            if self.enable_glow and self._breathing():
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

    def _apply_fallback_injections(self, sections, targets):
        jobs = []
        seen = set()
        for obj in targets:
            hash_val = _extract_hash_from_object(obj.name).lower()
            if not hash_val:
                print(f"[GlowFX] 跳过 {obj.name}: 无法解析哈希")
                continue
            suffix_pattern = _normalize_suffix(_extract_resource_suffix(obj.name))
            for section_name, units in self._collect_target_units(
                sections,
                obj,
                hash_val,
                suffix_pattern,
            ):
                for unit in units:
                    key = (section_name, int(unit["start"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    jobs.append((section_name, unit))

        changed = False
        for section_name, unit in sorted(
            jobs,
            key=lambda item: (item[0], int(item[1]["start"])),
            reverse=True,
        ):
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
                match = _LEGACY_GLOW_REF_RE.match(line.strip()) or _LOCAL_GLOW_REF_RE.match(line.strip())
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

        draw_index, draw_args = _first_draw_after(block, 0)
        if draw_index < 0:
            print(f"[GlowFX] 跳过 {unit.get('mesh', '')}: 找不到三参数 drawindexed")
            return False

        indent = _leading_whitespace(block[draw_index])
        # 只有「仅绘制发光贴图」才需要临时收起原始 draw；默认导出不包任何 guard，
        # 避免把 SlotFix 之类的运行时变量塞在原始 drawindexed 外面（未声明即风险）。
        call_lines = [
            _CALL_BEGIN,
        ]
        if self.emission_only:
            call_lines.append("endif")
        call_lines.extend([
            f"{GLOW_RESOURCE}{self._halo_suffix()} = ref {ref}",
            f"$glowfx_drawcount = {draw_args[0]}",
            f"$glowfx_drawstart = {draw_args[1]}",
            f"$glowfx_drawbase = {draw_args[2]}",
            RUN_LINE + self._halo_suffix(),
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
        call_lines.append(_CALL_END)
        _insert_block(block, draw_index + 1, call_lines, indent)

        param_lines = [_PARAM_BEGIN]
        if self.enable_glow:
            param_lines.extend(self._build_glow_parameter_lines())
        if self.emission_only:
            param_lines.extend([
                "$glowfx_emission_only = 1",
                "if !$glowfx_emission_only",
            ])
        param_lines.append(_PARAM_END)
        _insert_block(block, draw_index, param_lines, indent)

        lines[start:end] = block
        return True

    def _build_glow_parameter_lines(self):
        if self._breathing():
            _, values = self._split_breath_lines()
            return [_BREATH_BEGIN, *values, _BREATH_END]
        return [
            f"$glowfx_h = {_format_float(self.glow_h)}",
            f"$glowfx_s = {_format_float(self.glow_s)}",
            f"$glowfx_v = {_format_float(self.glow_v)}",
            f"$glowfx_brightness = {_format_float(self.glow_brightness)}",
            f"$glowfx_interpolate = {_format_float(self.glow_interpolate)}",
        ]

    def _build_breath_lines(self, breath_tick_count=1):
        return build_breath_parameter_lines(
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
            brightness_max=self.breath_brightness_max,
            interpolate=self.breath_interpolate,
            tick_count=breath_tick_count,
            smooth_combo=True,
        )

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
        normalization = 1.0
        spacing = 2 * max(1, (radius + 95) // 96)
        for offset in range(spacing, radius, spacing):
            fraction = offset / radius
            edge = 1.0 - fraction * fraction * (3.0 - 2.0 * fraction)
            normalization += 2.0 * 2.0 ** (-4.0 * fraction * fraction) * edge
        source = source.replace("__GLOW_RADIUS__", str(radius))
        source = source.replace("__GLOW_NORMALIZATION__", f"{normalization:.8f}")
        suffix = self._halo_suffix()
        if suffix:
            source = _RUNTIME_NAME_RE.sub(lambda match: match.group() + suffix, source)
        runtime, _ = self._parse_ini(source)
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

    def _copy_glow_fx_resources(self, mod_export_path):
        source_dir = os.path.join(self._resource_source_root(), "res")
        target_dir = os.path.join(mod_export_path, "res")
        os.makedirs(target_dir, exist_ok=True)
        for filename in (
            "glow_fx_ps.hlsl",
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
