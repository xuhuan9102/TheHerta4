"""Diffuse texture switching post-process node based on add-on V5.1."""

import glob
import os
import re
import shutil
import uuid
from collections import OrderedDict
from pathlib import Path

import bpy

from ..toolkit.tt_dds_conversion import (
    convert_texture_to_dds,
    existing_dds_matches_format,
    resolve_texture_dds_format,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base


NODE_IDNAME = "SSMTNode_PostProcess_DiffuseSwitch"

#: 角色激活标志候选。口径同 ``node_postprocess_custom_material_assign``、
#: ``anim_driver_base._get_activation_flag``（NTEMI 用 ``$ntmi_active0``）与
#: ``node_swap_ini`` 的 ``$active0``。
ACTIVATION_FLAG_CANDIDATES = ("$active0", "$ntmi_active0")
#: 只认这两种形态（行首 ``;`` 的注释行不算）：``global $active0`` 声明、
#: ``$active0 = 1`` 置位。两者任一在位即认为门控机制齐全（三件套同进同出：
#: 声明 + 部件置位 + 主 [Present] 清零）。
_ACTIVATION_FLAG_DECL_RE = re.compile(
    r"^[ \t]*global(?:[ \t]+persist)?[ \t]+(\$[A-Za-z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)
_ACTIVATION_FLAG_SET_RE = re.compile(
    r"^[ \t]*(\$[A-Za-z_]\w*)[ \t]*=[ \t]*1[ \t]*$", re.MULTILINE
)


def find_activation_flag_in_text(text):
    """返回 ini 文本里可用的角色激活标志（``$active0`` / ``$ntmi_active0``），没有则空串。

    判据取「门控机制在位」（声明或置位任一）而不是「猜游戏支不支持」：整套机制由
    导出器在「蓝图存在切换按键」时整体发射。没有这套机制时加
    ``condition = $active0 == 1`` 会让热键恒假（未声明变量按 0 读），所以宁可不加。
    """
    source = text or ""
    declared = set(_ACTIVATION_FLAG_DECL_RE.findall(source))
    assigned = set(_ACTIVATION_FLAG_SET_RE.findall(source))
    for candidate in ACTIVATION_FLAG_CANDIDATES:
        if candidate in declared or candidate in assigned:
            return candidate
    return ""


def abs_path(path):
    return Path(bpy.path.abspath(path)).resolve()


def read_ini(path):
    return Path(path).read_text(encoding="utf-8-sig")


def write_ini(path, text):
    Path(path).write_text(text, encoding="utf-8")


def parse_sections(text):
    matches = list(re.finditer(r"(?m)^(\[[^\r\n]+\])\s*$", text))
    result = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result.append((match.group(1)[1:-1], match.start(), end, text[match.start():end]))
    return result


def section_span(text, name):
    return re.search(
        r"(?ms)^(\[" + re.escape(name) + r"\]\s*$.*?)(?=^\[[^\r\n]+\]\s*$|\Z)",
        text,
    )


def find_resource_by_filename(text, wanted):
    wanted_norm = str(wanted).replace("\\", "/").lower()
    wanted_base = Path(wanted_norm).name.lower()
    for name, _, _, block in parse_sections(text):
        if not name.lower().startswith("resource"):
            continue
        match = re.search(r"(?mi)^\s*filename\s*=\s*(.+?)\s*$", block)
        if not match:
            continue
        filename = match.group(1).strip().replace("\\", "/")
        if filename.lower() == wanted_norm or Path(filename).name.lower() == wanted_base:
            return name, filename
    return None, None


def find_texture_overrides_for_resource(text, resource_name):
    assignment = _resource_assignment_pattern(resource_name)
    result = []
    for name, _, _, block in parse_sections(text):
        if not name.lower().startswith("textureoverride_"):
            continue
        hash_match = re.search(r"(?mi)^\s*hash\s*=\s*([0-9a-fA-F]+)\s*$", block)
        resource_match = assignment.search(block)
        if hash_match and resource_match:
            result.append((name, hash_match.group(1), block))
    return result


def _resource_assignment_pattern(resource_name):
    """Match legacy ``this =`` and any ZZMI texture assignment."""
    return re.compile(
        r"(?mi)^([ \t]*)(this|Resource\\ZZMI\\[A-Za-z0-9_]+)[ \t]*=[ \t]*"
        r"(ref[ \t]+)?" + re.escape(resource_name) + r"[ \t]*$"
    )


def _find_resource_assignment_style(block, resource_name=None):
    """Return ``(indent, lhs, ref_prefix)`` for an existing resource assignment."""
    candidates = []
    if resource_name:
        candidates.append(_resource_assignment_pattern(resource_name).search(block))
    candidates.extend([
        re.search(r"(?mi)^([ \t]*)(this|Resource\\ZZMI\\[A-Za-z0-9_]+)[ \t]*=[ \t]*(ref[ \t]+)?[^\r\n]+$", block),
    ])
    match = next((item for item in candidates if item), None)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3) or ""


def ensure_resource(text, name, filename):
    block_re = re.compile(
        r"(?ms)^\[" + re.escape(name) + r"\]\s*$.*?(?=^\[[^\r\n]+\]\s*$|\Z)"
    )
    new_block = f"[{name}]\nfilename = {filename}\n"
    match = block_re.search(text)
    if match:
        return text[:match.start()] + new_block + "\n" + text[match.end():]
    return text.rstrip() + "\n\n" + new_block


def copy_texture(texture_path, ini_path, export_as_dds=False, texture_type="", name_hints=()):
    source = abs_path(texture_path)
    if not source.is_file():
        raise FileNotFoundError(f"贴图不存在：{source}")
    texture_dir = Path(ini_path).resolve().parent / "Textures"
    texture_dir.mkdir(parents=True, exist_ok=True)

    # 生成 INI 时把非 DDS 源（编辑期无损 PNG 等）转成 DDS：游戏只认 DDS，而 INI 的
    # filename 直接取本函数返回值，所以文件名跟着变。转换失败回退为原样复制。
    if export_as_dds and source.suffix.lower() != ".dds":
        converted = texture_dir / f"{source.stem}.dds"
        # 「已存在 DDS」不能盲信：格式与当前规则期望不一致时必须重转，
        # 否则规则改对后旧线性 DDS 会被永远沿用。
        needs_conversion = not converted.exists()
        if not needs_conversion:
            expected_format = resolve_texture_dds_format(
                str(source),
                None,
                texture_type=texture_type,
                name_hints=name_hints,
            )
            needs_conversion = not existing_dds_matches_format(
                str(converted), expected_format
            )
        if needs_conversion:
            ok, reason = convert_texture_to_dds(
                str(source),
                str(converted),
                texture_type=texture_type,
                name_hints=name_hints,
            )
            if not ok:
                print(f"贴图转 DDS 失败，回退为原样复制: {source} - {reason}")
                converted = None
        if converted is not None and converted.exists():
            return f"Textures/{converted.name}".replace("\\", "/")

    destination = texture_dir / source.name
    if source != destination.resolve():
        shutil.copy2(source, destination)
    return f"Textures/{source.name}".replace("\\", "/")


def sanitize_identifier(value, fallback="Group_01"):
    result = re.sub(r"[^A-Za-z0-9_]", "_", str(value or "").strip())
    return result or fallback


def sanitize_key_for_var(key):
    return sanitize_identifier(key, "EMPTY")


def key_variable(key):
    """Return the variable name used by releases before per-group identities."""
    return "DiffuseSwap_Key_" + sanitize_key_for_var(key)


def diffuse_group_variable(group):
    """Return a stable control variable unique to one texture-switch group."""
    uid = str(getattr(group, "uid", "") or "").strip()
    if not uid:
        uid = uuid.uuid4().hex[:8]
        group.uid = uid
    return "DiffuseSwap_Group_" + sanitize_identifier(uid)


def diffuse_group_variable_name(group):
    return "$" + diffuse_group_variable(group)


def diffuse_group_hotkey(group):
    return str(getattr(group, "key", "") or "").strip()


def diffuse_group_option_count(group):
    return max(2, len(getattr(group, "new_textures", ())) + 1)


def ensure_constants_var(text, var):
    line = f"global persist ${var} = 0"
    constants = section_span(text, "Constants")
    if constants:
        block = constants.group(0)
        if re.search(r"(?m)^\s*" + re.escape(line) + r"\s*$", block):
            return text
        new_block = block.rstrip("\r\n") + "\n" + line + "\n\n"
        return text[:constants.start()] + new_block + text[constants.end():]
    return text.rstrip() + f"\n\n[Constants]\n{line}\n"


def ensure_keyswap(text, key, state_count, comment="", gui_guard="", var=None):
    var = var or key_variable(key)
    section_name = f"KeySwap_Diffuse_{var}"
    values = ",".join(str(index) for index in range(max(2, state_count)))
    condition = f"${var} == 0 || ${var} < {max(2, state_count)}"
    if gui_guard:
        condition = f"({condition}) && {gui_guard}"
    # 角色门控：与同 ini 的 [KeySwap_*] 物体切换 / 动画驱动 KeyToggle 同口径 ——
    # 只有角色当帧被绘制过（$active0 = 1）才响应热键，否则同一个键会把别的角色的
    # 贴图一起切走。缺激活标志时保持原样（加门控会让热键恒假）。
    activation_flag = find_activation_flag_in_text(text)
    if activation_flag:
        condition = f"{activation_flag} == 1 && ({condition})"
    lines = [f"[{section_name}]"]
    if comment:
        lines.append(f"; {comment}")
    lines.extend([
        f"condition = {condition}",
        f"key = {key}",
        "type = cycle",
        f"${var} = {values}",
    ])
    block = "\n".join(lines)

    # Migrate the pre-fix key-based section. Multiple groups may intentionally
    # share a hotkey, but each now needs its own state variable and KeySwap.
    legacy_section_name = f"KeySwap_Diffuse_{key_variable(key)}"
    if legacy_section_name != section_name:
        legacy = section_span(text, legacy_section_name)
        if legacy:
            text = text[:legacy.start()] + text[legacy.end():].lstrip("\r\n")

    existing = section_span(text, section_name)
    if existing:
        suffix = text[existing.end():].lstrip("\r\n")
        separator = "\n\n" if suffix else "\n"
        return text[:existing.start()] + block + separator + suffix
    return text.rstrip() + "\n\n" + block + "\n"


def make_switch_block(block, original_resource, replacement_resources, var, legacy_vars=()):
    generated = None
    for candidate_var in (var, *legacy_vars):
        generated = re.search(
            r"(?ms)^([ \t]*)if[ \t]+\$" + re.escape(candidate_var)
            + r"[ \t]*==[ \t]*0[ \t]*$.*?^\1endif[ \t]*$",
            block,
        )
        if generated:
            break
    original_assignment = _resource_assignment_pattern(original_resource).search(block)
    if (
        generated
        and original_assignment
        and not (generated.start() <= original_assignment.start() < generated.end())
    ):
        # Repair output produced by the earlier multi-group fix: it could wrap
        # the first ordinary draw condition after SetTextures while leaving the
        # original diffuse assignment in place. Remove that misplaced block and
        # regenerate at the original assignment location.
        cleaned = block[:generated.start()] + block[generated.end():]
        return make_switch_block(cleaned, original_resource, replacement_resources, var)
    # Only nest an existing switch block when it writes the same ZZMI slot.
    # Diffuse, LightMap, MaterialMap, etc. must remain independent blocks.
    target_style = _find_resource_assignment_style(block, original_resource)
    target_lhs = target_style[1] if target_style else None
    any_generated = generated
    if any_generated is None:
        # Only treat an existing conditional as a diffuse-switch block when
        # it actually assigns Resource\\ZZMI\\Diffuse.  Ordinary drawindexed
        # conditions in the same TextureOverride must not be mistaken for it.
        for candidate in re.finditer(
            r"(?ms)^([ \t]*)if[ \t]+\$[A-Za-z0-9_]+[ \t]*==[ \t]*0[ \t]*$.*?^\1endif[ \t]*$",
            block,
        ):
            candidate_text = candidate.group(0)
            if target_lhs and re.search(
                r"(?mi)^\s*" + re.escape(target_lhs) + r"\s*=\s*(?:ref\s+)?",
                candidate_text,
            ):
                any_generated = candidate
                break
    if generated is None and any_generated:
        # Another diffuse group already owns this override. Nest the new
        # group's selection around it so both groups remain independently
        # controllable instead of replacing the first group's block.
        indent = any_generated.group(1)
        style = _find_resource_assignment_style(block, original_resource)
        if not style:
            return None
        _, lhs, ref_prefix = style

        def assignment(resource):
            return f"{indent}    {lhs} = {ref_prefix}{resource}"

        nested = any_generated.group(0)
        nested_lines = nested.splitlines()
        lines = [f"{indent}if ${var} == 0"]
        lines.extend(f"{indent}    {line}" for line in nested_lines)
        for index, resource in enumerate(replacement_resources, start=1):
            lines.append(f"{indent}else if ${var} == {index}")
            lines.append(assignment(resource))
        if replacement_resources:
            lines.append(f"{indent}else")
            lines.append(assignment(replacement_resources[-1]))
        lines.append(f"{indent}endif")
        return block[:any_generated.start()] + "\n".join(lines) + block[any_generated.end():]

    if generated:
        indent = generated.group(1)
    else:
        style = _find_resource_assignment_style(block, original_resource)
        if not style:
            return None
        generated = _resource_assignment_pattern(original_resource).search(block)
        indent, lhs, ref_prefix = style

    if generated:
        style = _find_resource_assignment_style(block, original_resource)
    if not style:
        # A generated block may have no original-resource line after a manual edit.
        style = (indent, "this", "")
    indent, lhs, ref_prefix = style

    def assignment(resource):
        return f"{indent}    {lhs} = {ref_prefix}{resource}"

    lines = [f"{indent}if ${var} == 0", assignment(original_resource)]
    for index, resource in enumerate(replacement_resources, start=1):
        lines.append(f"{indent}else if ${var} == {index}")
        lines.append(assignment(resource))
    if replacement_resources:
        lines.append(f"{indent}else")
        lines.append(assignment(replacement_resources[-1]))
    lines.append(f"{indent}endif")
    replacement = "\n".join(lines)
    return block[:generated.start()] + replacement + block[generated.end():]


def apply_group(text, group, ini_path, owner_id):
    if not group.original_texture:
        raise ValueError(f"{group.name}：请先选择原贴图 A")
    if not group.new_textures:
        raise ValueError(f"{group.name}：至少添加一张切换贴图")
    if not diffuse_group_hotkey(group):
        raise ValueError(f"{group.name}：绑定按键不能为空")

    original_path = abs_path(group.original_texture)
    if not original_path.is_file():
        raise FileNotFoundError(f"{group.name}：原贴图不存在：{original_path}")
    original_resource, _ = find_resource_by_filename(text, group.original_texture)
    if not original_resource:
        raise ValueError(f"{group.name}：在 INI 中没有找到原贴图 A 对应的 Resource：{original_path.name}")

    targets = find_texture_overrides_for_resource(text, original_resource)
    if not targets:
        raise ValueError(f"{group.name}：找到 Resource {original_resource}，但没有对应 TextureOverride")
    if group.targets:
        selected = {target.section for target in group.targets if target.enabled}
        targets = [target for target in targets if target[0] in selected]
    if not targets:
        raise ValueError(f"{group.name}：没有启用的 TextureOverride 目标")

    group_uid = str(getattr(group, "uid", "") or "")
    resource_group_id = sanitize_identifier(f"{owner_id}_{group_uid}_{group.name}")
    replacement_resources = []
    for index, item in enumerate(group.new_textures, start=1):
        if not item.path:
            raise ValueError(f"{group.name}：状态 {index} 没有选择贴图")
        filename = copy_texture(
            item.path,
            ini_path,
            export_as_dds=bool(getattr(group, "export_as_dds", False)),
            texture_type="DiffuseMap",
            name_hints=["DiffuseMap"],
        )
        resource = f"Resource_DiffuseSwitch_{resource_group_id}_B{index}"
        text = ensure_resource(text, resource, filename)
        replacement_resources.append(resource)

    var = diffuse_group_variable(group)
    text = ensure_constants_var(text, var)
    changed = 0
    for section_name, _, _ in targets:
        match = section_span(text, section_name)
        if not match:
            continue
        new_block = make_switch_block(
            match.group(0),
            original_resource,
            replacement_resources,
            var,
            legacy_vars=(key_variable(group.key),),
        )
        if new_block is None:
            continue
        text = text[:match.start()] + new_block + text[match.end():]
        changed += 1
    if changed == 0:
        raise ValueError(f"{group.name}：没有修改任何 TextureOverride")
    return text, changed


class SSMT_DiffuseSwitchTexture(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty(name="切换贴图", subtype="FILE_PATH")


class SSMT_DiffuseSwitchTarget(bpy.types.PropertyGroup):
    enabled: bpy.props.BoolProperty(name="加入切换", default=True)
    section: bpy.props.StringProperty(name="TextureOverride")
    hash: bpy.props.StringProperty(name="Hash")
    resource: bpy.props.StringProperty(name="Resource")
    filename: bpy.props.StringProperty(name="原贴图")


class SSMT_DiffuseSwitchGroup(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="切换组", default="Group_01")
    comment: bpy.props.StringProperty(name="备注名", default="")
    key: bpy.props.StringProperty(name="绑定按键", default="N")
    ini_path: bpy.props.StringProperty(name="参考/手动修改 INI", subtype="FILE_PATH")
    original_texture: bpy.props.StringProperty(name="原贴图 A", subtype="FILE_PATH")
    new_textures: bpy.props.CollectionProperty(type=SSMT_DiffuseSwitchTexture)
    active_new_texture: bpy.props.IntProperty(default=0)
    targets: bpy.props.CollectionProperty(type=SSMT_DiffuseSwitchTarget)
    active_target: bpy.props.IntProperty(default=0)
    backup: bpy.props.BoolProperty(name="自动备份", default=True)
    copy_b: bpy.props.BoolProperty(name="统一使用相对路径", default=True)
    export_as_dds: bpy.props.BoolProperty(
        name="导出时把贴图转成DDS",
        description=(
            "生成 INI 时，本组的切换贴图若不是 .dds（例如编辑期用的无损 PNG），"
            "先用 texconv 按现有 DDS 转换规则转成 DDS 放进 MOD/Textures，"
            "INI 的 filename 也指向转出来的 DDS。源贴图文件保留不动。"
            "关闭时按原样复制源文件"
        ),
        default=True,
    )
    expanded: bpy.props.BoolProperty(name="展开", default=True)
    uid: bpy.props.StringProperty(default="", options={"HIDDEN"})


def _find_node(context, node_name):
    space = getattr(context, "space_data", None)
    if not space or space.type != "NODE_EDITOR":
        return None
    tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    node = tree.nodes.get(node_name) if tree else None
    return node if node and node.bl_idname == NODE_IDNAME else None


class SSMT_OT_DiffuseSwitch_AddGroup(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_add_group"
    bl_label = "添加切换组"
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        group = node.groups.add()
        group.name = f"Group_{len(node.groups):02d}"
        group.uid = uuid.uuid4().hex[:8]
        node.active_group = len(node.groups) - 1
        return {"FINISHED"}


class SSMT_OT_DiffuseSwitch_RemoveGroup(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_remove_group"
    bl_label = "删除切换组"
    node_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node or not 0 <= self.group_index < len(node.groups):
            return {"CANCELLED"}
        node.groups.remove(self.group_index)
        node.active_group = min(max(0, self.group_index), max(0, len(node.groups) - 1))
        return {"FINISHED"}


class SSMT_OT_DiffuseSwitch_AddTexture(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_add_texture"
    bl_label = "添加切换贴图"
    node_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty()
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node or not 0 <= self.group_index < len(node.groups):
            return {"CANCELLED"}
        group = node.groups[self.group_index]
        item = group.new_textures.add()
        item.path = self.filepath
        group.active_new_texture = len(group.new_textures) - 1
        return {"FINISHED"}


class SSMT_OT_DiffuseSwitch_RemoveTexture(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_remove_texture"
    bl_label = "删除切换贴图"
    node_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty()
    texture_index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node or not 0 <= self.group_index < len(node.groups):
            return {"CANCELLED"}
        textures = node.groups[self.group_index].new_textures
        if not 0 <= self.texture_index < len(textures):
            return {"CANCELLED"}
        textures.remove(self.texture_index)
        return {"FINISHED"}


class SSMT_OT_DiffuseSwitch_ScanTarget(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_scan_target"
    bl_label = "按原贴图定位目标"
    node_name: bpy.props.StringProperty()
    group_index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node or not 0 <= self.group_index < len(node.groups):
            return {"CANCELLED"}
        group = node.groups[self.group_index]
        path = node._shared_ini_path()
        if not path or not path.is_file():
            self.report({"ERROR"}, "请先选择可读取的参考 INI")
            return {"CANCELLED"}
        if not group.original_texture:
            self.report({"ERROR"}, "请先选择原贴图 A")
            return {"CANCELLED"}
        try:
            text = read_ini(path)
            resource, filename = find_resource_by_filename(text, group.original_texture)
            if not resource:
                raise ValueError(f"INI 中找不到原贴图：{Path(group.original_texture).name}")
            overrides = find_texture_overrides_for_resource(text, resource)
            group.targets.clear()
            for name, hash_value, _ in overrides:
                target = group.targets.add()
                target.section = name
                target.hash = hash_value
                target.resource = resource
                target.filename = filename
                target.enabled = True
            self.report({"INFO"}, f"定位成功：找到 {len(overrides)} 个 TextureOverride")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class SSMT_OT_DiffuseSwitch_Generate(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_generate"
    bl_label = "生成/修改指定 INI"
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        changed, targets, errors = node.generate_configured_ini_files()
        if errors:
            self.report({"ERROR"}, errors[0])
            return {"CANCELLED"}
        self.report({"INFO"}, f"完成：修改 {changed} 个 INI，处理 {targets} 个目标")
        return {"FINISHED"}


class SSMT_OT_DiffuseSwitch_Restore(bpy.types.Operator):
    bl_idname = "ssmt.diffuse_switch_restore"
    bl_label = "恢复备份"
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        count = node.restore_configured_backups()
        self.report({"INFO"}, f"恢复 {count} 个 INI")
        return {"FINISHED"}


class SSMTNode_PostProcess_DiffuseSwitch(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = "贴图切换 V5.1"
    bl_description = "生成多状态 Diffuse 贴图切换，并在 MOD 导出时自动写入 INI"

    groups: bpy.props.CollectionProperty(type=SSMT_DiffuseSwitchGroup)
    active_group: bpy.props.IntProperty(default=0)
    namespace: bpy.props.StringProperty(default="", options={"HIDDEN"})
    ini_path: bpy.props.StringProperty(name="参考/手动修改 INI", subtype="FILE_PATH")

    def init(self, context):
        super().init(context)
        self.width = 620
        group = self.groups.add()
        group.name = "Group_01"
        group.uid = uuid.uuid4().hex[:8]

    def copy(self, node):
        self.namespace = ""
        for group in self.groups:
            group.uid = uuid.uuid4().hex[:8]

    def _ensure_namespace(self):
        if not self.namespace:
            self.namespace = "dts_" + uuid.uuid4().hex[:8]
        return self.namespace

    def _shared_ini_path(self):
        """Return the node-wide INI, migrating the first legacy group path."""
        if self.ini_path:
            return abs_path(self.ini_path)
        for group in self.groups:
            if group.ini_path:
                return abs_path(group.ini_path)
        return None

    def draw_buttons(self, context, layout):
        if not self.ini_path:
            for legacy_group in self.groups:
                if legacy_group.ini_path:
                    self.ini_path = legacy_group.ini_path
                    break
        ini_box = layout.box()
        ini_box.label(text="共享 INI（所有切换组使用同一个文件）", icon="TEXT")
        ini_box.prop(self, "ini_path", text="参考/手动 INI")
        top = layout.row(align=True)
        top.operator("ssmt.diffuse_switch_add_group", text="添加切换组", icon="ADD").node_name = self.name
        top.operator("ssmt.diffuse_switch_generate", text="生成指定 INI", icon="FILE_TICK").node_name = self.name
        top.operator("ssmt.diffuse_switch_restore", text="恢复备份", icon="LOOP_BACK").node_name = self.name
        layout.label(text="连接到 MOD 后处理链后，导出时会自动写入生成的 INI", icon="INFO")

        for group_index, group in enumerate(self.groups):
            box = layout.box()
            header = box.row(align=True)
            header.prop(group, "expanded", text="", icon="TRIA_DOWN" if group.expanded else "TRIA_RIGHT", emboss=False)
            header.label(text=f"{group_index + 1}. {group.name}", icon="TEXTURE")
            remove = header.operator("ssmt.diffuse_switch_remove_group", text="", icon="X")
            remove.node_name = self.name
            remove.group_index = group_index
            if not group.expanded:
                continue

            identity = box.column(align=True)
            identity.prop(group, "name", text="组名")
            identity.prop(group, "comment", text="备注名")
            identity.prop(group, "key", text="绑定按键（原样写入 INI）")

            original = box.box()
            original.label(text="原贴图定位", icon="IMAGE_DATA")
            original.prop(group, "original_texture", text="原贴图 A")

            replacements = box.box()
            row = replacements.row(align=True)
            row.label(text="切换贴图", icon="TEXTURE")
            add = row.operator("ssmt.diffuse_switch_add_texture", text="添加", icon="ADD")
            add.node_name = self.name
            add.group_index = group_index
            for texture_index, item in enumerate(group.new_textures):
                row = replacements.row(align=True)
                row.label(text=f"状态 {texture_index + 1}")
                row.prop(item, "path", text="")
                remove_texture = row.operator("ssmt.diffuse_switch_remove_texture", text="", icon="X")
                remove_texture.node_name = self.name
                remove_texture.group_index = group_index
                remove_texture.texture_index = texture_index

            targets = box.box()
            targets.label(text="目标 TextureOverride", icon="TEXT")
            row = targets.row(align=True)
            scan = row.operator("ssmt.diffuse_switch_scan_target", text="", icon="VIEWZOOM")
            scan.node_name = self.name
            scan.group_index = group_index
            for target in group.targets:
                target_box = targets.box()
                row = target_box.row(align=True)
                row.prop(target, "enabled", text="")
                row.label(text=target.section)
                target_box.label(text=f"Hash: {target.hash}  Resource: {target.resource}")

            options = box.row(align=True)
            options.prop(group, "backup")
            options.label(text="切换贴图固定复制到 MOD/Textures 并使用相对路径", icon="CHECKMARK")
            box.prop(group, "export_as_dds")

    def _gui_only_guard(self):
        guards = []
        pending = [getattr(self, "id_data", None)]
        visited = set()
        while pending:
            tree = pending.pop()
            if tree is None or tree.name in visited:
                continue
            visited.add(tree.name)
            for node in tree.nodes:
                if node.bl_idname != "SSMTNode_PostProcess_SwapPanel" or node.mute:
                    continue
                if not bool(getattr(node, "gui_only", False)):
                    continue
                namespace = node._ensure_namespace() if hasattr(node, "_ensure_namespace") else ""
                if namespace:
                    guards.append(f"${namespace}_gui_only == 0")
            for candidate in bpy.data.node_groups:
                if getattr(candidate, "bl_idname", "") != "SSMTBlueprintTreeType":
                    continue
                for node in candidate.nodes:
                    if node.bl_idname != "SSMTNode_Blueprint_Nest" or node.mute:
                        continue
                    if str(getattr(node, "blueprint_name", "") or "") == tree.name:
                        pending.append(candidate)
                        break
        return " && ".join(guards)

    @staticmethod
    def _key_metadata(groups):
        metadata = OrderedDict()
        for group in groups:
            var = diffuse_group_variable(group)
            key = diffuse_group_hotkey(group)
            comment = str(group.comment or group.name or "").strip()
            metadata[var] = {
                "key": key,
                "state_count": diffuse_group_option_count(group),
                "comment": comment,
            }
        return metadata

    def _write_groups_to_path(self, path, groups, create_backup=False, gui_guard=""):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"INI 不存在：{path}")
        if create_backup:
            backup = Path(str(path) + ".diffuse_switch_backup")
            if not backup.exists():
                shutil.copy2(path, backup)
        original = read_ini(path)
        updated = original
        target_count = 0
        owner_id = self._ensure_namespace()
        key_metadata = self._key_metadata(groups)
        for group in groups:
            if not group.uid:
                group.uid = uuid.uuid4().hex[:8]
            updated, changed = apply_group(updated, group, path, owner_id)
            target_count += changed
        if key_metadata and not find_activation_flag_in_text(updated):
            print(
                "[贴图切换 V5.1] 未找到角色激活标志（global $active0）：切换热键无法做"
                "角色门控，同一个键会同时切换其它角色的贴图。"
            )
        for var, metadata in key_metadata.items():
            updated = ensure_keyswap(
                updated,
                metadata["key"],
                metadata["state_count"],
                metadata["comment"],
                gui_guard,
                var=var,
            )
        if updated != original:
            write_ini(path, updated)
            return True, target_count
        return False, target_count

    def generate_configured_ini_files(self):
        groups_by_path = OrderedDict()
        shared_path = self._shared_ini_path()
        if shared_path:
            groups_by_path[shared_path] = list(self.groups)
        changed = 0
        target_count = 0
        errors = []
        guard = self._gui_only_guard()
        for path, groups in groups_by_path.items():
            try:
                did_change, count = self._write_groups_to_path(
                    path, groups, any(group.backup for group in groups), guard
                )
                changed += int(did_change)
                target_count += count
            except Exception as exc:
                errors.append(str(exc))
        return changed, target_count, errors

    def restore_configured_backups(self):
        count = 0
        shared_path = self._shared_ini_path()
        paths = {shared_path} if shared_path else set()
        for path in paths:
            backup = Path(str(path) + ".diffuse_switch_backup")
            if backup.is_file():
                shutil.copy2(backup, path)
                count += 1
        return count

    def execute_postprocess(self, mod_export_path):
        ini_files = [Path(path) for path in sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))]
        if not ini_files or not self.groups:
            print("[贴图切换 V5.1] 未找到生成的 INI 或没有切换组，跳过")
            return False

        by_name = {path.name.lower(): path for path in ini_files}
        groups_by_target = OrderedDict()
        for group in self.groups:
            shared_path = self._shared_ini_path()
            configured_name = shared_path.name.lower() if shared_path else ""
            target = by_name.get(configured_name, ini_files[0])
            groups_by_target.setdefault(target, []).append(group)

        guard = self._gui_only_guard()
        success = True
        for target, groups in groups_by_target.items():
            try:
                if any(group.backup for group in groups):
                    self._create_cumulative_backup(str(target), mod_export_path)
                _, count = self._write_groups_to_path(target, groups, False, guard)
                print(f"[贴图切换 V5.1] 已写入 {target.name}，处理 {count} 个 TextureOverride")
            except Exception as exc:
                success = False
                print(f"[贴图切换 V5.1] 写入失败 {target}: {exc}")
        return success


classes = (
    SSMT_DiffuseSwitchTexture,
    SSMT_DiffuseSwitchTarget,
    SSMT_DiffuseSwitchGroup,
    SSMT_OT_DiffuseSwitch_AddGroup,
    SSMT_OT_DiffuseSwitch_RemoveGroup,
    SSMT_OT_DiffuseSwitch_AddTexture,
    SSMT_OT_DiffuseSwitch_RemoveTexture,
    SSMT_OT_DiffuseSwitch_ScanTarget,
    SSMT_OT_DiffuseSwitch_Generate,
    SSMT_OT_DiffuseSwitch_Restore,
    SSMTNode_PostProcess_DiffuseSwitch,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
