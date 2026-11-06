"""模型流光描边（FlowLight）后处理节点。

默认在 ZZZ 角色的正常材质绘制之后，以相同深度叠加边缘光与表面扫光。

分工：
- 参数与绘制解析在 ``flow_light_core``，着色器编译与门控在 ``flow_light_material``。
- 清旧严格限定在 ``tag`` 命名空间内，所以「同参数跑两次」必须逐字节相同。
"""

import bpy
import os
import glob
import re
from collections import OrderedDict

from . import flow_light_core as core
from . import flow_light_material as material
from .node_postprocess_base import SSMTNode_PostProcess_Base
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.safe_write import write_text_if_changed


# 遮罩重绘块的标记行：清旧时靠它定位整块（块内 if/clear/endif/set/run 都不带 tag）
FLOWLIGHT_MASK_MARKER = "; FLOWLIGHT_MASK"

# [Present] 链的外包标记（[Present] 段本身不能整段删，只能按标记区间清）
_FLOWLIGHT_PRESENT_BEGIN = "; FLOWLIGHT_BEGIN"
_FLOWLIGHT_PRESENT_END = "; FLOWLIGHT_END"

_TAG_INVALID_CHAR_PATTERN = re.compile(r'[^A-Za-z0-9_]')

#: 键位段名固定、不带 tag：一个 mod 里只该有一段，重复导出靠
#: ``_strip_old_flowlight`` 先删后建来保证幂等。
#: **没有默认键**：面板留空 = 不绑键、不生成这个段。
FLOWLIGHT_TOGGLE_SECTION = '[KeyFlowLightToggle]'

#: 总开关变量全名（1 = 显示流光 / 0 = 关闭，缺省 1）。
#: 刻意**不用 persist**：persist 会把运行值写进 d3dx_user.ini，下次启动拿旧值
#: 压住 ini 初值，「缺省 1」就不成立。先例见 fluid_ini_merge.py:1254-1258。
FLOWLIGHT_ENABLE_VAR = '$fl_enable'

#: 认宿主/上一轮留下的两种写法；``persist`` 组只用于迁移时去掉它。
_FL_ENABLE_DECL = re.compile(r'^global\s+(?:persist\s+)?\$fl_enable\s*=\s*(.*)$')


def sanitize_flow_light_tag(raw_tag):
    """把用户填的 tag 收敛成 ini 段落名里合法的标识符（非法字符换下划线）。"""
    cleaned = _TAG_INVALID_CHAR_PATTERN.sub('_', str(raw_tag or "").strip())
    return cleaned or "flowlight"


def normalise_toggle_key(spec):
    """把面板里的键位写法规范成 3Dmigoto 的 ``key =`` 值。

    与 ``fluid_ini_merge.normalise_key`` 同语义（这里不 import 流体模块，避免给
    流光节点多拖一条依赖链）：

    - 空白 / ``None`` → ``''``（**没有默认键**，调用方据此不生成段）；
    - 只写 ``VK_F7`` → 补 ``no_alt no_ctrl no_shift``；
    - 写了整串 → 压掉多余空白后原样用。

    任何输入都不抛异常：坏值最多让这一段键位难看，不能让整次导出挂掉。
    """
    text = " ".join(str(spec or "").split())
    if not text:
        return ''
    if " " not in text:
        text = "no_alt no_ctrl no_shift " + text
    return text


def build_toggle_key_section(spec):
    """``[KeyFlowLightToggle]`` 的**段体**（不含段头，段头由 sections 的 key 写）。"""
    return [
        '    key = ' + normalise_toggle_key(spec),
        '    type = cycle',
        '    ' + FLOWLIGHT_ENABLE_VAR + ' = 1,0',
    ]


class FlowLightObjectItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体", default="")  # type: ignore


class SSMTNode_PostProcess_FlowLight(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_FlowLight'
    bl_label = '模型流光描边'
    bl_description = '为选中物体生成模型边缘光与表面流光，沿用原材质遮挡（ZZZ）'

    effect_mode: bpy.props.EnumProperty(name="效果", items=(
        ('RIM', "边缘流光", "沿模型表面朝向产生菲涅耳边缘光"),
        ('SURFACE', "表面流光", "光带扫过模型表面"),
        ('BOTH', "边缘 + 表面", "同时启用边缘光与表面扫光"),
    ), default='BOTH')  # type: ignore
    surface_gain: bpy.props.FloatProperty(name="表面强度", default=0.35, min=0.0, max=4.0)  # type: ignore
    rim_power: bpy.props.FloatProperty(name="边缘收紧", default=4.0, min=0.1, max=32.0)  # type: ignore
    scan_axis: bpy.props.EnumProperty(name="模型扫光轴", items=(
        ('X', "X", "游戏模型局部 X 轴"), ('Y', "Y", "游戏模型局部 Y 轴"),
        ('Z', "Z", "游戏模型局部 Z 轴")), default='Z')  # type: ignore
    scan_min: bpy.props.FloatProperty(name="扫光起点", default=0.0)  # type: ignore
    scan_max: bpy.props.FloatProperty(name="扫光终点", default=1.8)  # type: ignore

    # ---------- 扫光 ----------
    scan_speed: bpy.props.FloatProperty(name="扫光速度", description="三角波频率（单程≈1/(2*speed) 秒）", default=0.0405, min=0.0001, max=1.0)  # type: ignore
    phase: bpy.props.FloatProperty(name="相位", description="扫光相位偏移（0..1）", default=0.0, min=0.0, max=1.0)  # type: ignore
    direction: bpy.props.EnumProperty(  # type: ignore
        name="扫光方向",
        items=(
            ('DOWN', "向下", "光带从上往下扫"),
            ('UP', "向上", "光带从下往上扫"),
        ),
        default='DOWN',
    )
    intensity: bpy.props.FloatProperty(name="亮度", description="光带亮度倍数", default=2.6, min=0.0, max=4.0)  # type: ignore
    core_width: bpy.props.FloatProperty(name="核心宽度", description="光带核心高斯 sigma（扫光范围归一化）", default=0.024, min=0.0001, max=0.5)  # type: ignore
    halo_width: bpy.props.FloatProperty(name="外晕宽度", description="光带外晕高斯 sigma", default=0.095, min=0.0001, max=1.0)  # type: ignore
    core_gain: bpy.props.FloatProperty(name="核心增益", default=2.2, min=0.0, max=10.0)  # type: ignore
    halo_gain: bpy.props.FloatProperty(name="外晕增益", default=0.18, min=0.0, max=10.0)  # type: ignore
    base_gain: bpy.props.FloatProperty(name="常亮底", description="没有光带经过时的底亮度", default=0.08, min=0.0, max=10.0)  # type: ignore

    # ---------- 颜色 ----------
    hue_speed: bpy.props.FloatProperty(name="色相滚动", default=0.45, min=0.0, max=5.0)  # type: ignore
    hue_bands: bpy.props.FloatProperty(name="彩虹分段", default=6.0, min=1.0, max=12.0)  # type: ignore
    core_white_mix: bpy.props.FloatProperty(name="核心混白", default=0.48, min=0.0, max=1.0)  # type: ignore
    emission_base: bpy.props.FloatProperty(name="自发光底", default=1.3, min=0.0, max=10.0)  # type: ignore
    emission_gain: bpy.props.FloatProperty(name="自发光增益", default=0.8, min=0.0, max=10.0)  # type: ignore
    emission_cap: bpy.props.FloatProperty(name="自发光上限", default=4.0, min=0.0, max=10.0)  # type: ignore

    # ---------- 命名空间与目标 ----------
    tag: bpy.props.StringProperty(name="段名标记", description="写入 ini 段名做命名空间，只允许 [A-Za-z0-9_]", default="flowlight")  # type: ignore
    key_toggle: bpy.props.StringProperty(  # type: ignore
        name="流光开关快捷键", default="",
        description="绑定 $fl_enable（1 = 显示流光 / 0 = 关闭，缺省 1）的全局快捷键。"
                    "留空 = 不绑键（没有默认键）；只写 VK_F7 会自动补 "
                    "no_alt no_ctrl no_shift；填了才会导出 [KeyFlowLightToggle] 段；"
                    "改完重新导出生效。",
    )
    flow_objects: bpy.props.CollectionProperty(type=FlowLightObjectItem)  # type: ignore
    active_flow_object: bpy.props.IntProperty(name="活动物体", default=0)  # type: ignore

    def draw_buttons(self, context, layout):
        del context
        box = layout.box()
        box.label(text="模型材质流光（ZZZ）", icon='MATERIAL')
        for prop in ("effect_mode", "surface_gain", "rim_power", "scan_axis", "scan_min", "scan_max"):
            box.prop(self, prop)
        box.label(text="边缘光位于模型表面内侧", icon='INFO')
        box = layout.box()
        box.label(text="扫光", icon='LIGHT')
        box.prop(self, "scan_speed")
        box.prop(self, "phase")
        box.prop(self, "direction")
        box.prop(self, "intensity")
        col = box.column(align=True)
        col.prop(self, "core_width")
        col.prop(self, "halo_width")
        col.prop(self, "core_gain")
        col.prop(self, "halo_gain")
        col.prop(self, "base_gain")

        box = layout.box()
        box.label(text="颜色", icon='COLOR')
        box.prop(self, "hue_speed")
        box.prop(self, "hue_bands")
        box.prop(self, "core_white_mix")
        col = box.column(align=True)
        col.prop(self, "emission_base")
        col.prop(self, "emission_gain")
        col.prop(self, "emission_cap")

        box = layout.box()
        box.label(text="导出设置", icon='IMAGE_DATA')
        box.prop(self, "tag")

        box = layout.box()
        box.label(text="流光开关快捷键", icon='KEYINGSET')
        box.prop(self, "key_toggle")
        key_spec = normalise_toggle_key(self.key_toggle)
        box.label(
            text=f"实际键位：{key_spec or '未绑定（留空 = 不绑键）'}", icon='INFO')
        box.label(text=f"→ {FLOWLIGHT_ENABLE_VAR}（1 = 显示流光 / 0 = 关闭）", icon='INFO')

        box = layout.box()
        box.label(text=f"目标物体（{len(self.flow_objects)}）", icon='OBJECT_DATA')
        for index, item in enumerate(self.flow_objects):
            row = box.row(align=True)
            row.prop(item, "object_name", text="")
            operator = row.operator("ssmt_postprocess.remove_flow_light_object", text="", icon='REMOVE')
            operator.node_name = self.name
            operator.index = index
        row = box.row(align=True)
        row.operator("ssmt_postprocess.add_flow_light_object", text="添加选中物体", icon='ADD').node_name = self.name
        row.operator("ssmt_postprocess.clear_flow_light_objects", text="一键清空", icon='TRASH').node_name = self.name

        resolved_count = getattr(self, "_last_resolved_draw_count", None)
        if resolved_count is None:
            box.label(text="尚未执行：运行一次后显示已解析 draw 数", icon='QUESTION')
        elif resolved_count:
            box.label(text=f"已解析 draw: {resolved_count}", icon='CHECKBOX_HLT')
        else:
            box.label(text="已解析 draw: 0（物体名与 ini 里的 mesh 对不上）", icon='CHECKBOX_DEHLT')

    # ------------------------------------------------------------------ #
    # ini 读写（语义照 node_postprocess_uv_offset 的 OrderedDict 契约）
    # ------------------------------------------------------------------ #

    def _read_ini_sections(self, ini_file_path):
        """→ ``(sections, preamble_lines, preserved_tail)``；文件不存在时 ``sections`` 为 None。

        两个必须保真的东西（流体探针兼容性，见 COMPAT-FLUID.md）：
        - **首个 ``[Section]`` 之前的行**（例如 ``namespace = Fluid_x``）：原样留在
          ``preamble_lines``，写出时先写它，不能丢；
        - **重复段名**（流体模板 ``Probe.ini`` 里 ``[Constants]`` 出现 5 次）：合并时
          把后续同名段的行**追加**到第一次出现的位置，不能覆盖（覆盖会删掉流体的
          ``$fluid_present`` 等声明，整条流体链静默死掉）。
        动画驱动块（``; --- ANIMATION DRIVER SECTION ---``）以首行注释为键留在
        ``sections`` 首位，写出时原样写回，不参与段解析、不重排。
        """
        sections = OrderedDict()
        preamble_lines = []
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as file_obj:
                content = file_obj.read()
        except FileNotFoundError:
            return None, [], ""

        anim_driver_content, content = self.split_anim_driver_block_content(content)
        content, preserved_tail_content = self.split_auto_appended_tail_content(content)

        if anim_driver_content:
            driver_lines = anim_driver_content.splitlines()
            if driver_lines:
                sections[driver_lines[0]] = driver_lines[1:]

        current_section = None
        for line in content.splitlines():
            stripped_line = line.strip()
            if stripped_line.startswith('[') and stripped_line.endswith(']'):
                current_section = stripped_line
                sections.setdefault(current_section, [])
                continue
            if current_section is None:
                # 段头之前的 namespace/注释：必须留，不能像旧实现那样丢弃
                if stripped_line or preamble_lines:
                    preamble_lines.append(line)
                continue
            sections[current_section].append(line)
        while preamble_lines and not preamble_lines[-1].strip():
            preamble_lines.pop()
        return sections, preamble_lines, preserved_tail_content

    def _write_ini_sections(self, sections, ini_file_path, preserved_tail_content="", preamble_lines=None):
        """按「段头 + 行 + 空行」写回；先写 preamble（namespace 等），段尾空行剥掉。"""
        body_lines = list(preamble_lines or [])
        if body_lines:
            body_lines.append("")
        for section_name, lines in sections.items():
            section_lines = list(lines or [])
            while section_lines and not str(section_lines[-1]).strip():
                section_lines.pop()
            body_lines.append(section_name)
            body_lines.extend(section_lines)
            body_lines.append("")

        text = "\n".join(body_lines)
        if preserved_tail_content:
            if not text.endswith("\n"):
                text += "\n"
            text += "\n" + preserved_tail_content
        return write_text_if_changed(ini_file_path, text)

    @staticmethod
    def _lookup_section_key(sections, section_name):
        """把 core 给的段名（可能不带方括号）对回 sections 里的真实键。"""
        raw_name = str(section_name or "").strip()
        if not raw_name:
            return None
        bare_name = raw_name.strip('[]')
        for candidate in (raw_name, f"[{bare_name}]"):
            if candidate in sections:
                return candidate
        target = bare_name.casefold()
        for key in sections:
            if str(key).strip('[]').casefold() == target:
                return key
        return None

    # ------------------------------------------------------------------ #
    # 清旧（严格限定 tag 命名空间）
    # ------------------------------------------------------------------ #


    def _strip_flowlight_lines(self, lines, tag):
        present_begin = f"{_FLOWLIGHT_PRESENT_BEGIN} {tag}"
        present_end = f"{_FLOWLIGHT_PRESENT_END} {tag}"
        # Migration cleanup for the retired screen-mask format.
        clear_shapes = {"if z248 == 0", f"clear = ResourceFLMask_{tag} 0.0 0.0 0.0 0.0", "endif"}
        set_shape = "z248 = 1"
        run_pattern = re.compile(rf"^run = CustomShaderFLMask_{re.escape(tag)}_\d+$")
        clear_pattern = re.compile(rf"^clear = ResourceFLMask_{re.escape(tag)}(?:\s|$)")

        kept_lines = []
        in_present_block = False
        index = 0
        total = len(lines)
        while index < total:
            stripped = str(lines[index] or "").strip()
            if stripped in tuple('; %s_BEGIN %s' % (prefix, tag) for prefix in (
                'FLOWLIGHT_MATERIAL_PRE', 'FLOWLIGHT_MATERIAL_POST',
                'FLOWLIGHT_CHARACTER_CONSTANTS', 'FLOWLIGHT_CHARACTER_RESET', 'FLOWLIGHT_CHARACTER_MARK')):
                end = stripped.replace('_BEGIN ', '_END ')
                index += 1
                while index < total and str(lines[index]).strip() != end:
                    index += 1
                index += 1
                continue
            if stripped == present_begin:
                in_present_block = True
                index += 1
                continue
            if stripped == present_end:
                in_present_block = False
                index += 1
                continue
            if in_present_block:
                index += 1
                continue
            if stripped.startswith(FLOWLIGHT_MASK_MARKER):
                if not any(run_pattern.fullmatch(str(line).strip()) for line in lines[index + 1:index + 7]):
                    kept_lines.append(lines[index])
                    index += 1
                    continue
                index += 1
                # 标记行之后的固定形状块：if z248 == 0 / clear / endif / z248 = 1 / run
                while index < total:
                    candidate = str(lines[index] or "").strip()
                    if candidate in clear_shapes or candidate == set_shape:
                        index += 1
                        continue
                    if run_pattern.fullmatch(candidate):
                        index += 1
                    break
                continue
            if clear_pattern.match(stripped) or run_pattern.fullmatch(stripped):
                index += 1
                continue
            kept_lines.append(lines[index])
            index += 1
        return kept_lines

    def _strip_old_flowlight(self, sections, tag):
        owned_section = re.compile(
            rf"^\[(?:CustomShaderFL(?:Mask|Material)_{re.escape(tag)}_\d+"
            rf"|CustomShaderFL(?:Seed|Jfa|Temporal|Composite)_{re.escape(tag)}"
            rf"|ResourceFL(?:Mask|JfaA|JfaB)_{re.escape(tag)}|PoolFLHistory_{re.escape(tag)})\]$")
        removed_sections = []
        # 总开关键位段固定名、不带 tag：每次都先删掉，存在与否只由本轮是否
        # 命中 draw 决定，重复导出因此只可能剩一段。
        if FLOWLIGHT_TOGGLE_SECTION in sections:
            del sections[FLOWLIGHT_TOGGLE_SECTION]
            removed_sections.append(FLOWLIGHT_TOGGLE_SECTION)
        for section_name in list(sections.keys()):
            if section_name == '[ShaderOverrideFLMaterialPass_%s]' % tag:
                del sections[section_name]
                removed_sections.append(section_name)
                continue
            if re.match(r'^\[ShaderRegexFLMaterial_' + re.escape(tag) + r'_\d+(?:\.[A-Za-z.]+)?\]$', section_name):
                del sections[section_name]
                removed_sections.append(section_name)
                continue
            if not owned_section.fullmatch(section_name):
                continue
            del sections[section_name]
            removed_sections.append(section_name)

        for section_name in list(sections.keys()):
            sections[section_name] = self._strip_flowlight_lines(sections[section_name] or [], tag)

        if removed_sections:
            print(f"[FlowLight] 清除旧段 {len(removed_sections)} 个: {', '.join(removed_sections)}")
        return removed_sections

    # ------------------------------------------------------------------ #
    # 物体 → draw 匹配
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_object_suffix(name):
        return re.sub(r'(_(?:chain|dup|copy|BPE)\d*)+$', '', str(name or ""), flags=re.IGNORECASE)

    def _extract_hash_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
            bare_unique_str = str(prefix_parts.get("bare_unique_str", "") or "").strip()
            if bare_unique_str:
                return bare_unique_str

        match = re.match(r'^([a-f0-9]{8}-[a-f0-9]+(?:-[a-f0-9]+)?)', str(obj_name or ""))
        if match:
            return match.group(1)
        match = re.match(r'^([a-f0-9]{8})', str(obj_name or ""))
        if match:
            return match.group(1)
        return None

    def _extract_hash_prefix(self, hash_val):
        if not hash_val:
            return None
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(hash_val)
        draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
        if draw_ib:
            return draw_ib
        normalized_hash_val = str(hash_val or "").strip()
        if normalized_hash_val.upper().startswith("LOD") and "." in normalized_hash_val:
            normalized_hash_val = normalized_hash_val.split(".", 1)[1]
        return normalized_hash_val.split('-')[0]

    def _extract_alias_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            _prefix, _separator, base_name = ObjectPrefixHelper.split_name_and_prefix(
                obj_name,
                prefix_info[0],
                prefix_info[1],
            )
            return base_name if base_name else obj_name

        obj_hash = self._extract_hash_from_name(obj_name)
        if obj_hash:
            remainder = str(obj_name or "")[len(obj_hash):]
            alias = remainder.lstrip('.').lstrip('_')
            return alias if alias else obj_name
        return obj_name

    def _match_draws_for_object(self, obj_name, draws_by_mesh):
        """先精确名字，再同前缀唯一别名，再同前缀唯一候选；多候选就跳过。"""
        if obj_name in draws_by_mesh:
            return draws_by_mesh[obj_name]

        obj_hash = self._extract_hash_from_name(obj_name)
        if not obj_hash:
            return []
        obj_prefix = self._extract_hash_prefix(obj_hash)
        if not obj_prefix:
            return []

        obj_alias_key = self._strip_object_suffix(self._extract_alias_from_name(obj_name)).casefold()
        obj_full_key = self._strip_object_suffix(obj_name).casefold()

        alias_hits = []
        prefix_hits = []
        for mesh_name, entries in draws_by_mesh.items():
            mesh_hash = self._extract_hash_from_name(mesh_name)
            if not mesh_hash:
                continue
            if self._extract_hash_prefix(mesh_hash) != obj_prefix:
                continue
            prefix_hits.append(mesh_name)
            mesh_alias_key = self._strip_object_suffix(self._extract_alias_from_name(mesh_name)).casefold()
            if (
                (mesh_alias_key and mesh_alias_key == obj_alias_key)
                or self._strip_object_suffix(mesh_name).casefold() == obj_full_key
            ):
                alias_hits.append(mesh_name)

        if len(alias_hits) == 1:
            return draws_by_mesh[alias_hits[0]]
        if len(alias_hits) > 1:
            print(f"[FlowLight] 物体 '{obj_name}' 的别名匹配到多个 draw 候选，跳过")
            return []
        if len(prefix_hits) == 1:
            return draws_by_mesh[prefix_hits[0]]
        if len(prefix_hits) > 1:
            print(f"[FlowLight] 物体 '{obj_name}' 同前缀有 {len(prefix_hits)} 个 draw 候选，跳过")
        return []

    def _resolve_draw_matches(self, sections, draws, object_names, tag):
        draws_by_mesh = OrderedDict()
        for draw in draws or []:
            mesh_name = str(draw.get("mesh_name") or "").strip()
            if not mesh_name:
                continue
            if draw.get("draw_params") is None:
                print(f"[FlowLight] mesh '{mesh_name}' 的绘制命令解析不到数字参数，跳过")
                continue
            draws_by_mesh.setdefault(mesh_name, []).append(draw)

        resolved = []
        seen_sites = set()
        for obj_name in object_names:
            hits = self._match_draws_for_object(obj_name, draws_by_mesh)
            if not hits:
                print(f"[FlowLight] 未找到物体 '{obj_name}' 对应的 draw，跳过")
                continue
            for draw in hits:
                section_key = self._lookup_section_key(sections, draw.get("section_name"))
                try:
                    line_index = int(draw.get("line_index", -1))
                except (TypeError, ValueError):
                    line_index = -1
                if section_key is None or line_index < 0:
                    print(f"[FlowLight] draw 定位失败，跳过: {draw.get('mesh_name')}")
                    continue
                # Scope the mesh's run call rather than instrumenting a shared
                # callee: another mesh can use that same command list.
                if draw.get("run_path"):
                    section_key, line_index = draw["run_path"][0]
                site_key = (section_key, line_index)
                if site_key in seen_sites:
                    continue
                seen_sites.add(site_key)
                matched = dict(draw)
                matched["section_key"] = section_key
                matched["line_index"] = line_index
                resolved.append(matched)

        section_order = {key: order for order, key in enumerate(sections.keys())}
        resolved.sort(key=lambda item: (section_order.get(item["section_key"], len(section_order)), item["line_index"]))
        print(f"[FlowLight] tag={tag} 命中 draw {len(resolved)} 条（目标物体 {len(object_names)} 个）")
        return resolved

    # ------------------------------------------------------------------ #
    # 插入 / 段落构造
    # ------------------------------------------------------------------ #



    def _apply_material(self, sections, resolved, tag, params, mod_export_path):
        if float(getattr(self, 'scan_max', 1.8)) <= float(getattr(self, 'scan_min', 0.0)):
            raise ValueError('流光扫光终点必须大于起点')
        with open(os.path.join(self._toolset_dir(), 'material.hlsl'), encoding='utf-8') as source:
            constants = core.shader_constants(params)
            constants.update({
                'FL_EFFECT_MODE': {'RIM': 0, 'SURFACE': 1, 'BOTH': 2}.get(getattr(self, 'effect_mode', 'BOTH'), 2),
                'FL_SURFACE_GAIN': float(getattr(self, 'surface_gain', 0.35)),
                'FL_RIM_POWER': float(getattr(self, 'rim_power', 4.0)),
                'FL_SCAN_AXIS': {'X': 0, 'Y': 1, 'Z': 2}.get(getattr(self, 'scan_axis', 'Z'), 2),
                'FL_SCAN_MIN': float(getattr(self, 'scan_min', 0.0)),
                'FL_SCAN_MAX': float(getattr(self, 'scan_max', 1.8)),
                'FL_MATERIAL_MAGIC': material.material_magic(tag),
            })
            source_text = core.inject_shader_constants(source.read(), constants)
            # Compile before changing the ini; a missing compiler/template fails
            # without leaving a new active shader chain in the mod.
            overlay_source = source_text
            material.compile_overlay(overlay_source)
        destination = os.path.join(mod_export_path, 'res', 'flow_light', tag + '.material.hlsl')
        write_text_if_changed(destination, overlay_source)
        for index, draw in reversed(list(enumerate(resolved))):
            lines = sections[draw['section_key']]
            site = draw['line_index']
            lines[site + 1:site + 1] = material.overlay_scope(tag, index)
        constants_lines = sections.setdefault('[Constants]', [])
        # 只留一条 plain `global $fl_enable = <原值>`：
        # - 旧导出/宿主可能写的是 `global persist ...`，persist 会把玩家按键值
        #   存进 d3dx_user.ini，下次启动压住 ini 初值（fluid_ini_merge.py:1254-1258
        #   故意不用 persist 的同一条理由），这里原地改写成 plain、保留原值；
        # - 重复声明只认第一条，其余丢掉，保证 [Constants] 里恰好一行；
        # - 一条都没有才补缺省 1。
        # 原地替换而不是删了再追加，否则 [Constants] 后面的 gate 行会让第二轮
        # 的段序变化，破坏逐字节幂等。
        enable_value = None
        normalized_constants = []
        for line in constants_lines:
            match = _FL_ENABLE_DECL.match(str(line).strip())
            if not match:
                normalized_constants.append(line)
                continue
            if enable_value is None:
                enable_value = (match.group(1) or '').strip() or '1'
                normalized_constants.append(
                    'global $fl_enable = ' + enable_value)
            # else: 同一变量的后续声明直接丢弃
        if enable_value is None:
            normalized_constants.append('global $fl_enable = 1')
        constants_lines[:] = normalized_constants
        gate_constants, gate_reset, gate_mark = material.character_blocks(tag)
        while constants_lines and not str(constants_lines[-1]).strip():
            constants_lines.pop()
        constants_lines.append('')
        constants_lines.extend(gate_constants)
        present_lines = sections.setdefault('[Present]', [])
        # Normalize section spacing so repeated export remains byte-identical.
        while present_lines and not str(present_lines[-1]).strip():
            present_lines.pop()
        present_lines.extend(gate_reset)
        for section_key in dict.fromkeys(draw['section_key'] for draw in resolved):
            sections[section_key][0:0] = gate_mark
        sections.update(material.build_overlay_sections(tag, resolved))
        # 放在最后：每次都是「先删后加」，段序稳定，重复导出逐字节相同。
        # 留空 = 不绑键：不写段（旧段已在 _strip_old_flowlight 里删掉）。
        key_spec = normalise_toggle_key(getattr(self, 'key_toggle', ''))
        if key_spec:
            sections[FLOWLIGHT_TOGGLE_SECTION] = build_toggle_key_section(key_spec)

    # ------------------------------------------------------------------ #
    # 着色器文件
    # ------------------------------------------------------------------ #

    @classmethod
    def _toolset_dir(cls):
        """模板目录；测试里 monkeypatch 这个方法指到临时目录，避免往仓库 Toolset 写。"""
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(addon_dir, "Toolset", "flow_light")


    @staticmethod
    def _remove_shader_files(mod_export_path, tag, keep_file_names):
        """清理本 tag 的旧源码与已退役的屏幕着色器缓存。"""
        res_dir = os.path.join(mod_export_path, "res", "flow_light")
        kept = {str(name) for name in keep_file_names or []}
        removed_count = 0
        candidates = set(glob.glob(os.path.join(res_dir, f"{tag}.*.hlsl")))
        for suffix in ('mask', 'composite', 'sdf_seed', 'sdf_jfa', 'sdf_temporal'):
            candidates.update(glob.glob(os.path.join(res_dir, f"{tag}.{suffix}.*")))
        for path in sorted(candidates):
            file_name = os.path.basename(path)
            if file_name in kept:
                continue
            try:
                os.remove(path)
                removed_count += 1
            except OSError as error:
                print(f"[FlowLight] 无法移除旧着色器 {file_name}: {error}")
        if removed_count:
            print(f"[FlowLight] 移除旧着色器 {removed_count} 个")
        return removed_count

    # ------------------------------------------------------------------ #
    # 参数
    # ------------------------------------------------------------------ #



    def _collect_params(self):
        """以 core.DEFAULT_PARAMS 的键为准取节点属性，缺项保持默认（公式只有 core 一份）。"""
        params = dict(core.DEFAULT_PARAMS)
        for key in list(params.keys()):
            if key == "direction":
                continue
            value = getattr(self, key, None)
            if value is None:
                continue
            try:
                params[key] = float(value)
            except (TypeError, ValueError):
                continue
        params["direction"] = -1.0 if str(getattr(self, "direction", "") or "").upper() == "UP" else 1.0

        return params

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    def _select_host_ini(self, ini_files, object_names):
        """在导出目录里挑「角色自己的配置表」，而不是按文件名排序取第一份。

        多节点共存时目录里会有 ``fluid_probe.ini`` 这类别的节点生成的 ini（它按字母序
        排在中文名前，旧实现会选中它，把流光段写进流体的文件、又在流体文件里找不到
        目标 draw，结果两边一起失效）。判据：有 ``[mesh:...]`` 绘制块、含目标物体哈希、
        或已有本节点旧产物（用于清理）的得分最高。
        """
        hashes = []
        for name in object_names or []:
            hash_value = self._extract_hash_from_name(name) or ""
            prefix = self._extract_hash_prefix(hash_value) or ""
            if prefix:
                hashes.append(prefix.casefold())

        best_path, best_score = None, -1
        for path in ini_files:
            try:
                with open(path, 'r', encoding='utf-8') as file_obj:
                    text = file_obj.read()
            except OSError:
                continue
            lowered = text.casefold()
            score = 0
            if "[mesh:" in lowered:
                score += 100
            for prefix in hashes:
                if prefix in lowered:
                    score += 10
            if "; flowlight_begin" in lowered:
                score += 50
            if score > best_score:
                best_path, best_score = path, score
        return best_path or ini_files[0]

    def execute_postprocess(self, mod_export_path):
        print(f"模型流光描边后处理节点开始执行，Mod导出路径: {mod_export_path}")
        tag = sanitize_flow_light_tag(getattr(self, "tag", ""))
        params = self._collect_params()
        object_names = [
            str(getattr(item, "object_name", "") or "").strip()
            for item in (getattr(self, "flow_objects", None) or [])
        ]
        object_names = [name for name in object_names if name]

        ini_files = sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))
        if not ini_files:
            print("[FlowLight] 路径中未找到任何 .ini 文件")
            return
        target_ini_file = self._select_host_ini(ini_files, object_names)
        print(f"[FlowLight] 目标配置表: {os.path.basename(target_ini_file)}")

        self._create_cumulative_backup(target_ini_file, mod_export_path)

        try:
            sections, preamble_lines, preserved_tail_content = self._read_ini_sections(target_ini_file)
            if not sections:
                print(f"[FlowLight] 配置表为空或不可读: {target_ini_file}")
                return

            had_flowlight = any(
                name.startswith(("[CustomShaderFL", "[ResourceFL", "[PoolFLHistory", "[ShaderRegexFLMaterial_"))
                for name in sections
            )
            self._strip_old_flowlight(sections, tag)

            draws = core.parse_draw_targets(sections)
            resolved = self._resolve_draw_matches(sections, draws, object_names, tag) if object_names else []
            self._last_resolved_draw_count = len(resolved)

            if resolved:
                self._apply_material(sections, resolved, tag, params, mod_export_path)
                self._remove_shader_files(mod_export_path, tag, [tag + '.material.hlsl'])
                print(f'[FlowLight] 已为 {len(resolved)} 条模型绘制生成材质流光（ZZZ）')
            else:
                self._remove_shader_files(mod_export_path, tag, [])
                print("[FlowLight] 没有命中任何 draw：只做了清旧，未生成新段落")

            if not resolved and not had_flowlight:
                return
            wrote_ini = self._write_ini_sections(
                sections, target_ini_file, preserved_tail_content, preamble_lines,
            )
            print(f"[FlowLight] 配置表已{'更新' if wrote_ini else '保持原样'}: {target_ini_file}")
        except Exception as error:
            print(f"生成模型流光描边配置时发生错误: {error}")
            import traceback
            traceback.print_exc()
            raise



class SSMT_OT_PostProcess_AddFlowLightObject(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.add_flow_light_object"
    bl_label = "添加选中物体"
    bl_description = "将当前选中的网格物体添加到流光描边物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_FlowLight':
            self.report({'ERROR'}, "请选择模型流光描边节点")
            return {'CANCELLED'}

        objects = [obj for obj in getattr(context, "selected_objects", []) if getattr(obj, "type", "") == 'MESH']
        if not objects and getattr(context, "active_object", None) is not None:
            objects = [context.active_object]
        if not objects:
            self.report({'WARNING'}, "请先选中要添加的物体")
            return {'CANCELLED'}

        existing = {str(item.object_name or "") for item in node.flow_objects}
        added_count = 0
        for obj in objects:
            if obj.name in existing:
                continue
            item = node.flow_objects.add()
            item.object_name = obj.name
            existing.add(obj.name)
            added_count += 1

        self.report({'INFO'}, f"已添加 {added_count} 个物体")
        return {'FINISHED'}


class SSMT_OT_PostProcess_RemoveFlowLightObject(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.remove_flow_light_object"
    bl_label = "删除物体"
    bl_description = "从流光描边物体列表中删除选中的物体"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_FlowLight':
            self.report({'ERROR'}, "请选择模型流光描边节点")
            return {'CANCELLED'}

        if 0 <= self.index < len(node.flow_objects):
            node.flow_objects.remove(self.index)
        return {'FINISHED'}


class SSMT_OT_PostProcess_ClearFlowLightObjects(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.clear_flow_light_objects"
    bl_label = "一键清空"
    bl_description = "清空流光描边物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        node = tree.nodes.get(self.node_name) if tree else None
        if not node or node.bl_idname != 'SSMTNode_PostProcess_FlowLight':
            self.report({'ERROR'}, "请选择模型流光描边节点")
            return {'CANCELLED'}

        while len(node.flow_objects) > 0:
            node.flow_objects.remove(len(node.flow_objects) - 1)
        self.report({'INFO'}, "已清空流光描边物体列表")
        return {'FINISHED'}


classes = (
    FlowLightObjectItem,
    SSMTNode_PostProcess_FlowLight,
    SSMT_OT_PostProcess_AddFlowLightObject,
    SSMT_OT_PostProcess_RemoveFlowLightObject,
    SSMT_OT_PostProcess_ClearFlowLightObjects,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
