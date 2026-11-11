import bpy
import os
import glob
import re
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base
from ..common.object_prefix_helper import ObjectPrefixHelper


def _strip_lod(name: str) -> str:
    match = re.match(r'^LOD\d+\.(.+)$', name, re.IGNORECASE)
    return match.group(1) if match else name


def _extract_hash_from_object(object_name: str) -> str:
    if not object_name:
        return ""
    bare = _strip_lod(object_name)
    prefix_info = ObjectPrefixHelper.extract_prefix_info(bare)
    prefix = prefix_info[0] if prefix_info else bare.split(".", 1)[0]
    match = re.match(r'^([a-f0-9]{8})', prefix, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r'([a-f0-9]{8})', prefix)
    return match.group(1) if match else prefix


def _extract_resource_suffix(object_name: str) -> str:
    if not object_name:
        return ""
    bare = _strip_lod(object_name)
    prefix_info = ObjectPrefixHelper.extract_prefix_info(bare)
    if prefix_info:
        return prefix_info[0]
    match = re.match(r'^([a-f0-9]{8}(?:-[a-f0-9]+)*)', bare, re.IGNORECASE)
    if match:
        return match.group(1)
    return bare.split(".", 1)[0]


class RabbitFXTargetItem(bpy.types.PropertyGroup):
    def _update_target(self, context):
        obj = bpy.data.objects.get(self.target_object)
        if obj:
            self.target_hash = _extract_hash_from_object(obj.name)
            self.resource_suffix = _extract_resource_suffix(obj.name)
        else:
            self.target_hash = ""
            self.resource_suffix = ""

    target_object: bpy.props.StringProperty(
        name="物体", description="选择要处理的物体（自动提取哈希值）",
        default="", update=_update_target,
    )
    target_hash: bpy.props.StringProperty(name="哈希", default="")
    resource_suffix: bpy.props.StringProperty(name="标识", default="")


# 2026-11-12 起由「材质转资源pro → RabbitFX贴图后处理pro」链替代。
# 以下 _SSMTNode_PostProcess_GlowLegacyImpl 是旧版实现的完整副本：
# 仅保留代码参考，不注册、不调用；不要在这里继续修 bug。
class _SSMTNode_PostProcess_GlowLegacyImpl(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_Glow'
    bl_label = 'RabbitFX贴图后处理'
    bl_description = '为指定物体添加 RabbitFX 发光/镂空/颜色偏移/W-Engine同步（支持多目标）'

    # ── 多目标物体选择 ──
    target_items: bpy.props.CollectionProperty(type=RabbitFXTargetItem)
    active_target_index: bpy.props.IntProperty(name="当前目标", default=0, min=0)

    # ── 发光 Glow ──
    enable_glow: bpy.props.BoolProperty(name="发光 Glow", default=True)
    glow_brightness: bpy.props.FloatProperty(name="光晕范围", default=8.0, min=0, max=100)
    glow_h: bpy.props.FloatProperty(name="H", default=0.0, min=-360, max=360)
    glow_s: bpy.props.FloatProperty(name="S", default=0.0, min=-100, max=100)
    glow_v: bpy.props.FloatProperty(name="V", default=0.0, min=-100, max=100)
    glow_interpolate: bpy.props.FloatProperty(name="插值", default=1.0, min=0, max=1)

    # ── 镂空 FXMap Cutout ──
    enable_fxmap: bpy.props.BoolProperty(name="镂空 FXMap", default=False)

    # ── W-Engine 同步 ──
    enable_sync: bpy.props.BoolProperty(name="W-Engine同步", default=False)
    sync_blendmode: bpy.props.IntProperty(name="混合模式", default=1, min=0, max=1)

    # ── 颜色偏移 ColorShift ──
    enable_colorshift: bpy.props.BoolProperty(name="颜色偏移 ColorShift", default=False)
    cs_h: bpy.props.FloatProperty(name="H", default=0.0, min=-360, max=360)
    cs_s: bpy.props.FloatProperty(name="S", default=0.0, min=-100, max=100)
    cs_v: bpy.props.FloatProperty(name="V", default=0.0, min=-100, max=100)

    # ── 呼吸灯系统 ──
    breath_mode: bpy.props.EnumProperty(
        name="呼吸模式",
        items=[
            ('OFF', "关闭", "不使用呼吸灯"),
            ('SINGLE', "单色呼吸", "固定颜色，光晕强度周期性脉动"),
            ('RAINBOW', "多彩渐变", "Hue 全色环循环，光晕范围固定"),
            ('COMBO', "渐变式呼吸", "Hue 全色环循环 + 光晕同步脉动"),
        ],
        default='OFF',
    )
    breath_fps: bpy.props.IntProperty(name="周期(帧)", default=200, min=60, max=7200)
    breath_step: bpy.props.FloatProperty(name="速度", default=0.3, min=0.1, max=100)
    breath_h_speed: bpy.props.FloatProperty(name="Hue速度", default=5.0, min=1, max=100)
    breath_h: bpy.props.FloatProperty(name="H", default=0.0, min=0, max=360)
    breath_s: bpy.props.FloatProperty(name="S", default=0, min=0, max=100)
    breath_v: bpy.props.FloatProperty(name="V", default=0, min=0, max=100)
    breath_brightness_max: bpy.props.FloatProperty(name="最大光晕", default=8.0, min=0, max=500)
    breath_brightness_min: bpy.props.FloatProperty(name="最小光晕", default=1.0, min=0, max=500)
    breath_interpolate: bpy.props.FloatProperty(name="插值", default=1.0, min=0, max=1)
    bloom_boost: bpy.props.FloatProperty(name="光晕范围倍率", default=0.5, min=0.5, max=20.0, description="值越大光晕范围扩散越明显")

    # ── UI 折叠 ──
    show_glow: bpy.props.BoolProperty(default=True)
    show_fxmap: bpy.props.BoolProperty(default=False)
    show_sync: bpy.props.BoolProperty(default=False)
    show_colorshift: bpy.props.BoolProperty(default=False)
    show_breath: bpy.props.BoolProperty(default=False)

    def draw_buttons(self, context, layout):
        # ── 多目标列表 ──
        row = layout.row()
        row.label(text="目标物体列表:", icon='OUTLINER_OB_GROUP_INSTANCE')
        row.operator("ssmt.rabbitfx_target_add", text="", icon='ADD')
        row.operator("ssmt.rabbitfx_target_remove", text="", icon='REMOVE')

        if not self.target_items:
            layout.label(text="请添加至少一个目标物体", icon='ERROR')
        else:
            col = layout.column(align=True)
            for i, item in enumerate(self.target_items):
                box = col.box()
                # Header row with index and hash info
                header = box.row(align=True)
                icon = 'TRIA_RIGHT' if i != self.active_target_index else 'TRIA_DOWN'
                op = header.operator("ssmt.rabbitfx_target_select", text=f"目标 {i+1}", icon=icon)
                op.index = i
                if item.target_hash:
                    header.label(text=f"哈希: {item.target_hash}")
                else:
                    header.label(text="未选择", icon='ERROR')

                # Expanded view for active target
                if i == self.active_target_index:
                    row = box.row(align=True)
                    row.prop_search(item, "target_object", bpy.data, "objects", text="物体", icon='OBJECT_DATA')
                    if item.target_object:
                        obj = bpy.data.objects.get(item.target_object)
                        row.label(text="", icon='MESH_DATA' if obj else 'ERROR')
                    if item.target_hash:
                        box.label(text=f"哈希: {item.target_hash}  标识: {item.resource_suffix}", icon='INFO')

        # ── Glow ──
        row = layout.row(align=True)
        row.prop(self, "show_glow", icon='TRIA_DOWN' if self.show_glow else 'TRIA_RIGHT', emboss=False)
        row.prop(self, "enable_glow", text="发光 Glow")
        if self.show_glow:
            col = layout.column(align=True)
            breathing = self.breath_mode != 'OFF'
            col.enabled = not breathing
            col.prop(self, "glow_brightness", slider=True)
            r = col.row(align=True)
            r.prop(self, "glow_h"); r.prop(self, "glow_s"); r.prop(self, "glow_v")
            col.prop(self, "glow_interpolate", slider=True)
            if breathing:
                col.label(text="呼吸灯启用中，静态值被覆盖", icon='INFO')

        # ── 呼吸灯 ──
        row = layout.row(align=True)
        row.prop(self, "show_breath", icon='TRIA_DOWN' if self.show_breath else 'TRIA_RIGHT', emboss=False)
        row.label(text="呼吸灯", icon='ANIM')
        if self.show_breath:
            col = layout.column(align=True)
            col.prop(self, "breath_mode", text="模式")
            mode = self.breath_mode
            if mode != 'OFF':
                col.prop(self, "breath_fps")
                col.prop(self, "breath_step")
                is_single = mode == 'SINGLE'
                is_rainbow = mode == 'RAINBOW'
                is_combo = mode == 'COMBO'
                if is_single:
                    r = col.row(align=True)
                    r.prop(self, "breath_h"); r.prop(self, "breath_s"); r.prop(self, "breath_v")
                    col.prop(self, "breath_brightness_min")
                    col.prop(self, "breath_brightness_max")
                if is_rainbow or is_combo:
                    r = col.row(align=True)
                    r.prop(self, "breath_s"); r.prop(self, "breath_v")
                if is_rainbow:
                    col.prop(self, "breath_brightness_max", text="光晕范围")
                if is_combo:
                    col.prop(self, "breath_brightness_min")
                    col.prop(self, "breath_brightness_max")
                col.prop(self, "breath_interpolate", slider=True)
                col.prop(self, "bloom_boost", slider=True)

        # ── FXMap ──
        row = layout.row(align=True)
        row.prop(self, "show_fxmap", icon='TRIA_DOWN' if self.show_fxmap else 'TRIA_RIGHT', emboss=False)
        row.prop(self, "enable_fxmap", text="镂空 FXMap")
        if self.show_fxmap:
            layout.label(text="Alpha=0 的区域被裁切", icon='INFO')

        # ── Sync ──
        row = layout.row(align=True)
        row.prop(self, "show_sync", icon='TRIA_DOWN' if self.show_sync else 'TRIA_RIGHT', emboss=False)
        row.prop(self, "enable_sync", text="W-Engine同步")
        if self.show_sync:
            layout.prop(self, "sync_blendmode", text="混合模式")

        # ── ColorShift ──
        row = layout.row(align=True)
        row.prop(self, "show_colorshift", icon='TRIA_DOWN' if self.show_colorshift else 'TRIA_RIGHT', emboss=False)
        row.prop(self, "enable_colorshift", text="颜色偏移 ColorShift")
        if self.show_colorshift:
            layout.label(text="FXMap 的 R 通道作为偏移遮罩", icon='INFO')
            r = layout.row(align=True)
            r.prop(self, "cs_h"); r.prop(self, "cs_s"); r.prop(self, "cs_v")

    def _parse_ini(self, ini_file_path, content=None):
        sections = OrderedDict()
        cur = None
        preamble = []
        try:
            if content is None:
                with open(ini_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
            for line in content.splitlines():
                s = line.strip()
                if s.startswith('[') and s.endswith(']'):
                    cur = s
                    sections[cur] = []
                elif cur is not None:
                    sections[cur].append(line.rstrip())
                else:
                    preamble.append(line.rstrip())
        except FileNotFoundError:
            return None
        return sections, preamble

    @classmethod
    def split_anim_driver_block_content(cls, content):
        """Extract a complete animation-driver block from the top of an INI file.

        Mirrors SSMTNode_PostProcess_Base.split_anim_driver_block_content on
        main so this branch works standalone (the method was added to the base
        class after this PR forked).
        """
        text = str(content or "")
        lines = text.splitlines(keepends=True)
        start_index = next(
            (index for index, line in enumerate(lines)
             if getattr(cls, "ANIM_DRIVER_SECTION_MARKER_START", "; --- ANIMATION DRIVER SECTION ---") in line),
            None,
        )
        if start_index is None:
            return "", text
        end_marker = getattr(cls, "ANIM_DRIVER_SECTION_MARKER_END", "; --- END ANIMATION DRIVER SECTION ---")
        end_index = next(
            (index for index in range(start_index + 1, len(lines)) if end_marker in lines[index]),
            None,
        )
        if end_index is None:
            return "", text
        driver_content = "".join(lines[start_index:end_index + 1])
        remaining_content = "".join(lines[end_index + 1:]).lstrip("\r\n")
        return driver_content, remaining_content

    def _process_single_target(self, ini_file, mod_export_path, hash_val, suffix):
        """处理单个目标哈希值"""
        content = open(ini_file, 'r', encoding='utf-8').read()
        preserved_driver_content, content = self.split_anim_driver_block_content(content)
        content, tail = self.split_auto_appended_tail_content(content)
        sections, preamble = self._parse_ini(ini_file, content)
        if sections is None:
            return

        target_key = None
        candidates = []
        for sk in sections:
            if not sk.startswith('[TextureOverride'):
                continue
            for line in sections[sk]:
                s = line.strip()
                if s.startswith('hash ='):
                    if s.split('=', 1)[1].strip().lower() == hash_val:
                        candidates.append(sk)
                        break
        if not candidates:
            print(f"[RabbitFX] 未找到 hash={hash_val}，跳过")
            return

        # 用 resource_suffix 构建精确节名匹配模式（节名使用下划线分隔）
        suffix_pattern = suffix.replace('-', '_')

        # 优先1：match_first_index + 节名包含完整 suffix_pattern
        for sk in candidates:
            if suffix_pattern in sk:
                lines_text = '\n'.join(sections[sk])
                if 'match_first_index' in lines_text:
                    target_key = sk
                    break

        # 优先2：match_first_index（原逻辑，向后兼容）
        if not target_key:
            for sk in candidates:
                lines_text = '\n'.join(sections[sk])
                if 'match_first_index' in lines_text:
                    target_key = sk
                    break

        # 优先3：drawindexed/Draw + 节名包含完整 suffix_pattern
        if not target_key:
            for sk in candidates:
                if suffix_pattern in sk:
                    lines_text = '\n'.join(sections[sk])
                    if 'drawindexed' in lines_text or 'Draw =' in lines_text:
                        target_key = sk
                        break

        # 优先4：drawindexed/Draw（原逻辑，向后兼容）
        if not target_key:
            for sk in candidates:
                lines_text = '\n'.join(sections[sk])
                if 'drawindexed' in lines_text or 'Draw =' in lines_text:
                    target_key = sk
                    break

        # 优先5：节名包含完整 suffix_pattern（仅此一个候选项时有用）
        if not target_key:
            for sk in candidates:
                if suffix_pattern in sk:
                    target_key = sk
                    break

        # 兜底：取第一个候选项
        if not target_key:
            target_key = candidates[0]

        print(f"[RabbitFX] 匹配: {target_key}")
        lines = sections[target_key]

        # 清理已有的 RabbitFX 行
        markers = {
            '; === RabbitFX Glow ===', '; === \u7ed3\u675f Glow ===',
            '; === RabbitFX FXMap ===', '; === \u7ed3\u675f FXMap ===',
            '; === RabbitFX Sync ===', '; === \u7ed3\u675f Sync ===',
            '; === RabbitFX ColorShift ===', '; === \u7ed3\u675f ColorShift ===',
        }
        remove_idx = set()
        rabbitfx_if_depth = 0
        for i, line in enumerate(lines):
            s = line.strip()
            remove = False
            if (s.startswith('Resource\\RabbitFX\\') or s.startswith('$\\RabbitFX\\')
                    or s.startswith('$\\rabbitfx\\') or s.startswith('run = CommandList\\RabbitFX\\')
                    or s.startswith('$framevar_') or s.startswith('$glaic_') or s.startswith('$valve_')
                    or s.startswith('; === Breath') or s.startswith('; === \u7ed3\u675f Breath')
                    or s.startswith('; === Rainbow') or s.startswith('; === \u7ed3\u675f Rainbow')
                    or s in markers):
                remove = True
            elif s.startswith('if $framevar_') or s.startswith('if $valve_'):
                remove = True
                rabbitfx_if_depth += 1
            elif s == 'else' and rabbitfx_if_depth > 0:
                remove = True
            elif s == 'endif' and rabbitfx_if_depth > 0:
                remove = True
                rabbitfx_if_depth -= 1
            if remove:
                remove_idx.add(i)
        lines[:] = [line for i, line in enumerate(lines) if i not in remove_idx]

        # 构建插入行
        insert_lines = []
        new_resource_sections = OrderedDict()

        mode = self.breath_mode
        breathing = mode != 'OFF'
        use_static_glow = self.enable_glow and not breathing

        if use_static_glow:
            rname = f"Resource-{suffix}-Glowmap"
            insert_lines.append("; === RabbitFX Glow ===")
            insert_lines.append(f"Resource\\RabbitFX\\Glowmap = ref {rname}")
            insert_lines.append(f"$\\RabbitFX\\H = {self.glow_h}")
            insert_lines.append(f"$\\RabbitFX\\S = {self.glow_s}")
            insert_lines.append(f"$\\RabbitFX\\V = {self.glow_v}")
            insert_lines.append(f"$\\RabbitFX\\brightness = {self.glow_brightness}")
            insert_lines.append(f"$\\RabbitFX\\interpolate = {self.glow_interpolate}")
            insert_lines.append("; === \u7ed3\u675f Glow ===")
            new_resource_sections[f"[{rname}]"] = [f"filename = Textures/{suffix}-Glowmap.dds"]

        if breathing:
            tag = hash_val.replace('-', '_')
            rname = f"Resource-{suffix}-Glowmap"
            fps = self.breath_fps
            step = self.breath_step
            boost = self.bloom_boost

            insert_lines.append(f"; === Breath mode={mode} ===")
            insert_lines.append(f"$framevar_{tag} = $framevar_{tag} + {step}")
            insert_lines.append(f"if $framevar_{tag} >= {fps}")
            insert_lines.append(f"    $framevar_{tag} = $framevar_{tag} - {fps}")
            insert_lines.append(f"    $valve_{tag} = 1 - $valve_{tag}")
            insert_lines.append("endif")
            insert_lines.append(f"if $valve_{tag} == 0")
            insert_lines.append(f"    $glaic_{tag} = $framevar_{tag}")
            insert_lines.append("else")
            insert_lines.append(f"    $glaic_{tag} = {fps} - $framevar_{tag}")
            insert_lines.append("endif")

            if mode == 'SINGLE':
                bmin = self.breath_brightness_min * boost
                bmax = self.breath_brightness_max * boost
                insert_lines.append(f"$\\RabbitFX\\H = {self.breath_h}")
                insert_lines.append(f"$\\RabbitFX\\S = {self.breath_s}")
                insert_lines.append(f"$\\RabbitFX\\V = {self.breath_v}")
                insert_lines.append(f"$\\RabbitFX\\brightness = {bmin} + ({bmax} - {bmin}) * $glaic_{tag} / {fps}")
                insert_lines.append(f"$\\RabbitFX\\interpolate = {self.breath_interpolate}")
            elif mode == 'RAINBOW':
                br = self.breath_brightness_max * boost
                h_ratio = 360.0 / fps
                insert_lines.append(f"$\\RabbitFX\\H = $glaic_{tag} * {h_ratio}")
                insert_lines.append(f"$\\RabbitFX\\S = {self.breath_s}")
                insert_lines.append(f"$\\RabbitFX\\V = {self.breath_v}")
                insert_lines.append(f"$\\RabbitFX\\brightness = {br}")
                insert_lines.append(f"$\\RabbitFX\\interpolate = {self.breath_interpolate}")
            elif mode == 'COMBO':
                bmax = self.breath_brightness_max * boost
                h_ratio = 360.0 / fps
                insert_lines.append(f"$\\RabbitFX\\H = $glaic_{tag} * {h_ratio}")
                insert_lines.append(f"$\\RabbitFX\\S = {self.breath_s}")
                insert_lines.append(f"$\\RabbitFX\\V = {self.breath_v}")
                insert_lines.append(f"$\\RabbitFX\\brightness = $glaic_{tag} * {bmax / fps}")
                insert_lines.append(f"$\\RabbitFX\\interpolate = {self.breath_interpolate}")

            insert_lines.append(f"Resource\\RabbitFX\\Glowmap = ref {rname}")
            insert_lines.append("; === \u7ed3\u675f Breath ===")
            new_resource_sections[f"[{rname}]"] = [f"filename = Textures/{suffix}-Glowmap.dds"]
            # Constants 声明
            const_lines = [
                f"global persist $framevar_{tag} = 0",
                f"global $valve_{tag} = 0",
                f"global $glaic_{tag} = 0",
            ]
            if '[Constants]' in sections:
                existing = sections['[Constants]']
                existing_set = {l.strip() for l in existing}
                for cl in const_lines:
                    if cl not in existing_set:
                        existing.append(cl)
            else:
                sections['[Constants]'] = const_lines

        if self.enable_fxmap:
            rname = f"Resource-{suffix}-FXMap"
            insert_lines.append("; === RabbitFX FXMap ===")
            insert_lines.append(f"Resource\\RabbitFX\\FXMap = ref {rname}")
            insert_lines.append("; === \u7ed3\u675f FXMap ===")
            new_resource_sections[f"[{rname}]"] = [f"filename = Textures/{suffix}-FXMap.dds"]

        if self.enable_glow or self.enable_fxmap:
            insert_lines.append("run = CommandList\\RabbitFX\\Run")

        if self.enable_sync:
            rname = f"Resource-{suffix}-FXBuffer"
            insert_lines.append("; === RabbitFX Sync ===")
            insert_lines.append(f"$\\rabbitfx\\blendmode = {self.sync_blendmode}")
            insert_lines.append(f"Resource\\RabbitFX\\SetFXBuffer = ref {rname}")
            insert_lines.append("; === \u7ed3\u675f Sync ===")
            new_resource_sections[f"[{rname}]"] = [
                "type = Buffer", "filename = Textures/black.dds",
            ]

        if self.enable_colorshift:
            rname = f"Resource-{suffix}-CSMap"
            insert_lines.append("; === RabbitFX ColorShift ===")
            insert_lines.append(f"Resource\\RabbitFX\\FXMap = ref {rname}")
            insert_lines.append(f"$\\RabbitFX\\H = {self.cs_h}")
            insert_lines.append(f"$\\RabbitFX\\S = {self.cs_s}")
            insert_lines.append(f"$\\RabbitFX\\V = {self.cs_v}")
            insert_lines.append("run = CommandList\\RabbitFX\\ColorShift")
            insert_lines.append("; === \u7ed3\u675f ColorShift ===")
            new_resource_sections[f"[{rname}]"] = [f"filename = Textures/{suffix}-CSMap.png"]

        if not insert_lines:
            return

        ins_idx = -1
        for i, line in enumerate(lines):
            s = line.strip()
            if (s.startswith('Resource\\ZZMI\\NormalMap')
                    or s.startswith('Resource\\RabbitFX\\NormalMap')
                    or s.startswith('Resource\\NTEMIFX\\NormalMap')):
                ins_idx = i
                break
        if ins_idx == -1:
            for i, line in enumerate(lines):
                if 'drawindexed' in line or 'Draw =' in line:
                    ins_idx = i
                    break
        if ins_idx == -1:
            ins_idx = len(lines)

        for ln in reversed(insert_lines):
            lines.insert(ins_idx, ln)
        sections[target_key] = lines

        for sec_name, sec_lines in new_resource_sections.items():
            if sec_name not in sections:
                sections[sec_name] = sec_lines

        with open(ini_file, 'w', encoding='utf-8') as f:
            if preserved_driver_content:
                f.write(preserved_driver_content)
                if not preserved_driver_content.endswith('\n'):
                    f.write('\n')
                f.write('\n')
            for line in preamble:
                f.write(line + '\n')
            for sk, sl in sections.items():
                if sk.startswith('['):
                    f.write(f"{sk}\n")
                for line in sl:
                    f.write(f"{line}\n")
                f.write("\n")
            if tail:
                f.write(tail)

        print(f"[RabbitFX] ✅ hash={hash_val}")

    def execute_postprocess(self, mod_export_path):
        valid_targets = [
            item for item in self.target_items
            if item.target_hash and item.resource_suffix
        ]
        if not valid_targets:
            print("[RabbitFX] 错误: 未选择任何有效目标物体")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return

        for ini_file in ini_files:
            self._create_cumulative_backup(ini_file, mod_export_path)
            for item in valid_targets:
                hash_val = item.target_hash.strip().lower()
                suffix = item.resource_suffix.strip()
                self._process_single_target(ini_file, mod_export_path, hash_val, suffix)


class SSMTNode_PostProcess_Glow(SSMTNode_PostProcess_Base):
    """RabbitFX 贴图后处理（弃用壳）。

    仅保留 bl_idname 用于加载旧版蓝图文件，避免未知节点类型导致节点被
    Blender 静默丢弃。功能已由「材质转资源pro → RabbitFX贴图后处理pro」链
    承担；与「材质转资源」不同，本节点没有自动迁移，需要手动替换。
    """
    bl_idname = 'SSMTNode_PostProcess_Glow'
    bl_label = 'RabbitFX贴图后处理（已弃用）'
    bl_description = '已弃用：功能由「材质转资源pro → RabbitFX贴图后处理pro」链承担；本节点不会自动转换，请手动替换。'

    def draw_buttons(self, context, layout):
        layout.label(
            text='已弃用：不会自动转换；请改用「材质转资源pro → RabbitFX贴图后处理pro」链',
            icon='ERROR',
        )

    def execute_postprocess(self, mod_export_path):
        # 弃用壳：显式 no-op。基类这里会 raise NotImplementedError，旧 .blend
        # 里挂着本节点时会让整条导出链失败，所以必须在这里堵死导出入口。
        print(
            '[RabbitFX] 已弃用节点，跳过导出：请改用'
            '「材质转资源pro → RabbitFX贴图后处理pro」链'
        )


# ── 旧版目标列表运算符（仅服务未注册的旧实现；不再注册）──
class SSMT_OT_RabbitFX_TargetAdd(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_target_add"
    bl_label = "添加目标"
    bl_description = "添加一个新的目标物体"

    def execute(self, context):
        node = context.active_node
        if node and node.bl_idname == 'SSMTNode_PostProcess_Glow':
            item = node.target_items.add()
            node.active_target_index = len(node.target_items) - 1
        return {'FINISHED'}


class SSMT_OT_RabbitFX_TargetRemove(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_target_remove"
    bl_label = "移除目标"
    bl_description = "移除当前选中的目标物体"

    def execute(self, context):
        node = context.active_node
        if node and node.bl_idname == 'SSMTNode_PostProcess_Glow':
            idx = node.active_target_index
            if 0 <= idx < len(node.target_items):
                node.target_items.remove(idx)
                node.active_target_index = max(0, min(idx, len(node.target_items) - 1))
        return {'FINISHED'}


class SSMT_OT_RabbitFX_TargetSelect(bpy.types.Operator):
    bl_idname = "ssmt.rabbitfx_target_select"
    bl_label = "选择目标"
    bl_description = "展开/折叠此目标"

    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        node = context.active_node
        if node and node.bl_idname == 'SSMTNode_PostProcess_Glow':
            if node.active_target_index == self.index:
                node.active_target_index = -1  # 折叠
            else:
                node.active_target_index = self.index
        return {'FINISHED'}


# 只注册弃用壳；旧实现及其 PropertyGroup/运算符保留定义但不注册。
_classes = (SSMTNode_PostProcess_Glow,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
