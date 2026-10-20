# -*- coding: utf-8 -*-
"""物体贴图替换与未引用贴图清理后处理节点（无损新增，不改动既有节点）。

功能：
1. 扫描当前蓝图节点树（含嵌套蓝图）上引用的网格物体，形成节点内的物体列表。
2. 为列表中的每个物体指定一张 Diffuse 贴图文件。
3. MOD 导出（execute_postprocess）时，在生成的 INI 中按物体定位对应的
   ``; [mesh:...]`` 贴图引用区块（TextureOverride_LOD0.xxx 等），把该区块引用的
   ``Resource-...-DiffuseMap`` 定义段改写为指向设定的贴图。
4. 将设定的贴图复制到 mod 的 ``Textures`` 目录。
5. 清理 ``Textures`` 目录中不被任何 ini 配置文件引用的其它贴图文件。

约定与其它后处理节点一致：
- 以 ``; [mesh:...]`` 注释作为「物体 <-> INI 区块」的映射依据；
- 只改写目标 Resource 定义段的 ``filename`` 行（引用行保持不变）；
- 其余内容、动画驱动块与自动追加尾段一律原样保留（按行原位替换）。
"""

import glob
import os
import re
import shutil

import bpy

from ..toolkit.tt_dds_conversion import (
    convert_texture_to_dds,
    existing_dds_matches_format,
    resolve_texture_dds_format,
)
from .node_postprocess_base import SSMTNode_PostProcess_Base


NODE_IDNAME = "SSMTNode_PostProcess_ObjectTextureAssign"
NODE_LABEL = "物体贴图替换与清理"

# --------------------------------------------------------------------------- #
# ini / 路径基础工具
# --------------------------------------------------------------------------- #

_TEXTURE_FOLDER = "Textures"

# 每个物体的贴图槽： (item 属性名, 显示标签, ini 资源后缀, 引用行左侧参数)
_MAP_SLOTS = (
    ("diffuse_path", "Diffuse", "DiffuseMap", "Diffuse"),
    ("normal_path", "Normal", "NormalMap", "NormalMap"),
    ("light_path", "Light", "LightMap", "LightMap"),
    ("material_path", "Material", "MaterialMap", "MaterialMap"),
)

_MESH_COMMENT_RE = re.compile(r";\s*\[mesh:([^\]]+)\]", re.IGNORECASE)
_SECTION_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_ASSIGN_RE = re.compile(
    r"^\s*(?P<param>Resource\\[A-Za-z0-9_]+\\[A-Za-z0-9_]+|this|ps-t\d+)\s*=\s*"
    r"(?:ref\s+)?(?P<resource>[A-Za-z0-9_\-\.]+)\s*(?:;.*)?$",
    re.IGNORECASE,
)
_FILENAME_RE = re.compile(
    r"^(?P<prefix>[ \t]*filename[ \t]*=[ \t]*)(?P<value>.+?)(?P<trailing>[ \t]*)$",
    re.IGNORECASE,
)

# 物体导出时可能附带的运行时后缀（顺序无关，重复剥离）
_SUFFIX_PATTERNS = (
    re.compile(r"_copy$"),
    re.compile(r"_dup\d+$"),
    re.compile(r"_chain\d+$"),
)
_LOD_PREFIX_RE = re.compile(r"^LOD\d+\.", re.IGNORECASE)
_HASH_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{6,}[-_]\d+[-_]\d+\.")
# 匹配 mesh 注释/物体名开头的 LOD 前缀，如 LOD0.6b25e6d8-30336-0（兼容 _ 分隔）
_LOD_TOKEN_RE = re.compile(r"^LOD\d+[.][0-9a-fA-F]{6,}[-_]\d+[-_]\d+", re.IGNORECASE)


def _detect_eol(text):
    if "\r\n" in text:
        return "\r\n"
    return "\n"


def _read_text(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def _write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _split_lines(text):
    """拆行并保留换行符，方便按原位精确改行。"""
    return text.splitlines(keepends=True)


def _norm_rel(name):
    """把 INI 中的 filename 值规范化为小写正斜杠相对路径（用于引用比对）。"""
    return str(name or "").strip().replace("\\", "/").lower()


def _resolve_inside(root, relative):
    """把 mod 根目录下的相对路径解析为绝对路径；越界返回 None。"""
    root_abs = os.path.realpath(os.path.abspath(root))
    candidate = os.path.realpath(os.path.abspath(os.path.join(root_abs, str(relative).replace("\\", os.sep))))
    try:
        if os.path.commonpath((os.path.normcase(root_abs), os.path.normcase(candidate))) != os.path.normcase(root_abs):
            return None
    except ValueError:
        return None
    return candidate


# --------------------------------------------------------------------------- #
# 物体 <-> [mesh:] 名称匹配
# --------------------------------------------------------------------------- #

def _strip_runtime_suffixes(name):
    current = str(name or "").strip()
    for _ in range(16):
        changed = False
        for pattern in _SUFFIX_PATTERNS:
            new_name = pattern.sub("", current)
            if new_name != current:
                current = new_name
                changed = True
                break
        if not changed:
            break
    return current


def _object_identity_candidates(obj):
    """由场景物体推导其在导出 ini 中可能出现的完整路径标识。"""
    if obj is None:
        return []
    chain = []
    current = obj
    depth = 0
    while current is not None and depth < 32:
        chain.insert(0, current.name)
        if current.parent is None:
            break
        current = current.parent
        depth += 1
    dotted = ".".join(chain)
    candidates = {obj.name, dotted}
    # 场景/导出时的运行时副本名可能带 _copy/_dup/_chain 后缀，一并纳入匹配
    for name in list(candidates):
        stripped = _strip_runtime_suffixes(name)
        if stripped and stripped != name:
            candidates.add(stripped)
    return [name for name in candidates if name]


def _normalize_lod(value):
    """把 LOD 前缀里的 _ / - / 大小写统一，便于比对。"""
    return re.sub(r"[-_]", "-", str(value or "").strip()).lower()


def _lod_token(value):
    """提取开头的 LOD 前缀（含 LODx.），如 ``LOD0.6b25e6d8-30336-0``；找不到返回 ""。"""
    match = _LOD_TOKEN_RE.match(str(value or "").strip())
    return match.group(0).strip() if match else ""


def _lod_core(value):
    """去掉 ``LODx.`` 后的 hash-首帧-数量，如 ``6b25e6d8-30336-0``。"""
    token = _lod_token(value)
    return re.sub(r"^LOD\d+[.]", "", token, flags=re.IGNORECASE) if token else ""


def _entry_match_key(entry):
    """给物体条目生成匹配键：优先用其 LOD 前缀（LODx.hash-fi-ic）。

    若多个物体共享同一 LOD 前缀，它们在处理时会被视为同一个匹配目标
    （避免重复改写、也避免同前缀物体被分到不同组时互相冲突）。
    无 LOD 前缀时退回物体名（去后缀）。
    """
    name = str(getattr(entry, "object_name", "") or "").strip()
    token = _lod_token(name)
    if token:
        return _normalize_lod(token)
    base = _normalize_lod(_strip_runtime_suffixes(name))
    return base or _normalize_lod(name)


def _mesh_base_name(mesh_name):
    """把 [mesh:...] 注释归一化为可比较的路径（去 LOD/hash 前缀与运行时后缀）。

    例如 ``LOD0.1300e048-55575-0.上半身.奶子.001_copy`` -> ``上半身.奶子.001``。
    """
    value = str(mesh_name or "").strip()
    if not value:
        return ""
    value = _strip_runtime_suffixes(value)
    value = _LOD_PREFIX_RE.sub("", value)
    value = _HASH_PREFIX_RE.sub("", value)
    return value


def _identity_candidates_from_name(name):
    """仅靠物体名字符串推导候选标识（不依赖物体是否存在于场景）。

    注意事项：Blender 自动重名后缀会出现在名字里（如 ``奶子.001``），
    因此这里**绝不**按 '.' 拆出裸数字段（否则会把 ``.001`` 当成 ``001`` 造成串段）。
    只保留：完整名、以及去运行时后缀（_copy/_dup/_chain）后的名称。
    """
    name = str(name or "").strip()
    candidates = set()
    if not name:
        return candidates
    candidates.add(name)
    stripped = _strip_runtime_suffixes(name)
    if stripped:
        candidates.add(stripped)
    return candidates


def _mesh_comment_matches(mesh_name, identity_names):
    """mesh 注释是否命中任一身份候选。

    规则（按用户明确要求）：从物体名里只取出开头的 ``LODx.xxxxxxx-xxxxx-xxxx``
    前缀（如 ``LOD0.5eb66b57-26148-0``），把它作为关键词去定位 ini 中形如
    ``; [mesh:LOD0.5eb66b57-26148-0`` 的网格注释；前缀之后的 ``.头.马尾``、
    ``_copy`` 等一律忽略。兼容 ``_``/``-`` 分隔与大小写。

    当身份名本身没有 LOD 前缀（如裸子物体名）时，退回到完整名/后缀匹配
    （用完整名，不取裸数字，避免串到别的 ``.001``）。
    """
    # 1) 收集所有身份候选的 LOD 前缀（含/不含 LODx. 两种写法）
    prefixes = set()
    for ident in identity_names:
        ident = str(ident or "").strip()
        if not ident:
            continue
        tok = _lod_token(ident)
        if tok:
            prefixes.add(_normalize_lod(tok))
        core = _lod_core(ident)
        if core:
            prefixes.add(_normalize_lod(core))
        # 身份名本身就是完整前缀字符串（如 LOD0.5eb66b57-26148-0）
        prefixes.add(_normalize_lod(ident))

    # 2) 只要 mesh 注释开头的前缀落在前缀集合里就算命中
    if prefixes:
        mesh_tok = _lod_token(mesh_name)
        if mesh_tok and _normalize_lod(mesh_tok) in prefixes:
            return True
        mesh_core = _lod_core(mesh_name)
        if mesh_core and _normalize_lod(mesh_core) in prefixes:
            return True

    # 3) 无 LOD 前缀可用的身份：回退到完整名/后缀匹配
    base = _mesh_base_name(mesh_name)
    if base:
        for ident in identity_names:
            ident = str(ident or "").strip()
            if not ident:
                continue
            if ident == base or base.endswith("." + ident):
                return True
    return False


# --------------------------------------------------------------------------- #
# ini 结构扫描
# --------------------------------------------------------------------------- #

def _collect_texture_override_sections(lines):
    """返回 [(section_name, [body_line_index...]), ...]（仅 TextureOverride_* 段）。"""
    sections = []
    current_name = None
    body = []
    pending = []
    for index, line in enumerate(lines):
        header = _SECTION_HEADER_RE.match(line.strip())
        if header:
            if current_name is not None:
                pending.append((current_name, body))
            current_name = header.group(1).strip()
            body = []
            continue
        if current_name is not None:
            body.append(index)
    if current_name is not None:
        pending.append((current_name, body))

    for name, body_indices in pending:
        if name.lower().startswith("textureoverride_"):
            sections.append((name, body_indices))
    return sections


def _iter_mesh_names_in_section(lines, body_indices):
    for index in body_indices:
        match = _MESH_COMMENT_RE.search(lines[index])
        if match:
            yield index, match.group(1).strip()


def _diffuse_resource_in_section(lines, body_indices):
    return _resource_for_map_type(lines, body_indices, "DiffuseMap")


def _resource_for_map_type(lines, body_indices, suffix):
    """返回该 TextureOverride 段内引用指定地图类型（后缀，如 DiffuseMap）的资源名。"""
    suffix_lower = str(suffix or "").lower()
    fallback = None
    for index in body_indices:
        match = _ASSIGN_RE.match(lines[index].strip())
        if not match:
            continue
        resource = match.group("resource").strip()
        if suffix_lower and resource.lower().endswith(suffix_lower):
            return resource
        param_lower = match.group("param").lower()
        key = suffix_lower.replace("map", "")
        if fallback is None and key and key in param_lower:
            fallback = resource
    return fallback


def _find_resource_filename_index(lines, resource_name):
    """在 lines 中定位 [resource_name] 段里 filename 行的下标。返回 (index, prefix)。"""
    if not resource_name:
        return None, ""
    wanted = "[" + str(resource_name).strip() + "]"
    current = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
            continue
        if current is None or current != wanted:
            continue
        match = _FILENAME_RE.match(stripped)
        if match:
            return index, match.group("prefix")
    return None, ""


# --------------------------------------------------------------------------- #
# 运行时：替换一个 ini 中每个物体的贴图引用
# --------------------------------------------------------------------------- #

# 匹配 TextureOverride 段里的贴图引用行，如：
#   Resource\ZZMI\Diffuse = ref Resource-9fbf4911-11472-0-DiffuseMap
#   Resource\EFMI\Diffuse = ref Resource-xxx-DiffuseMap   （兼容其他游戏命名空间）
_ASSIGN_LINE_RE = re.compile(
    r"^(?:Resource\\(?P<ns>[A-Za-z0-9_]+)\\(?P<slot>[A-Za-z0-9_]+)|\b(?P<lhs_this>this)\b)"
    r"[ \t]*=[ \t]*(?:(?P<ref>ref)[ \t]+)?"
    r"(?P<target>[A-Za-z0-9_\-\.]+)[ \t]*(?P<trail>;.*)?$",
    re.IGNORECASE,
)


def _resource_name_for_texture(source_path, label, suffix):
    """为新贴图生成稳定资源名，形如 ``Resource_1300e048_720_55575_Diffuse``。

    去掉贴图文件名的扩展名与末尾的任意 ``-DiffuseMap/-NormalMap/...`` 尾巴，
    再把非标识符字符替换为 ``_``，最后拼上槽标签。同贴图多槽复用同基名。
    """
    stem = os.path.splitext(os.path.basename(source_path))[0]
    for _s in ("diffusemap", "normalmap", "lightmap", "materialmap"):
        if stem.lower().endswith("-" + _s):
            stem = stem[: -(len(_s) + 1)]
            break
    stem = re.sub(r"[^A-Za-z0-9_]", "_", stem)
    return f"Resource_{stem}_{label}"


def _ensure_resource_block(lines, resource_name, filename, eol):
    """确保 ``[resource_name]`` 段存在且其 ``filename`` 为给定值（就地修改 lines）。

    新段插入在末尾 ``;sha256=...`` 行之前；若已存在则就地更新其 filename。
    """
    wanted = "[" + resource_name + "]"
    for i, line in enumerate(lines):
        if line.strip() == wanted:
            j = i + 1
            while j < len(lines) and not (lines[j].strip().startswith("[") and lines[j].strip().endswith("]")):
                if _FILENAME_RE.match(lines[j].strip()):
                    indent = lines[j][: len(lines[j]) - len(lines[j].lstrip(" \t"))]
                    trailing = lines[j][len(lines[j].rstrip(" \t\r\n")):]
                    lines[j] = f"{indent}filename = {filename}{trailing}"
                    return
                j += 1
            lines.insert(j, f"filename = {filename}{eol}")
            return
    # 插入点：优先放在首个 ;sha256 行之前，否则文件末尾
    insert_at = len(lines)
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(";sha256"):
            insert_at = i
            break
    block = [eol, f"{wanted}{eol}", f"filename = {filename}{eol}"]
    # 若插入点前一行为空（或文件开头），去掉多余的首空行
    if insert_at == 0 or (insert_at - 1 < len(lines) and lines[insert_at - 1].strip() == ""):
        block = block[1:]
    lines[insert_at:insert_at] = block


def _replace_assignment_ref(lines, body_indices, param, new_resource, eol):
    """把段内 ``Resource\\<ns>\\<param> = ref <旧>`` 的目标替换为 new_resource。

    只按槽名(param，如 Diffuse/NormalMap/LightMap/MaterialMap)匹配；命名空间(ZZMI/EFMI/
    WWMI...)不写死，原命名空间与槽名原样保留。返回改动行数。
    """
    param_lower = param.lower()
    count = 0
    for index in body_indices:
        line = lines[index]
        m = _ASSIGN_LINE_RE.match(line.strip())
        if not m or m.group("lhs_this"):
            continue
        if m.group("slot").lower() != param_lower:
            continue
        ns = m.group("ns")
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        trailing = line[len(line.rstrip(" \t\r\n")):]
        ref_prefix = "ref " if m.group("ref") else ""
        lines[index] = f"{indent}Resource\\{ns}\\{m.group('slot')} = {ref_prefix}{new_resource}{trailing}"
        count += 1
    return count


def _remove_resource_block_if_unreferenced(lines, resource_name):
    """若整个 ini 中已没有 ``ref <resource_name>``，则移除其 [resource_name] 段。返回是否移除。"""
    if any(re.search(r"(?i)\bref\s+" + re.escape(resource_name) + r"\b", line) for line in lines):
        return False
    wanted = "[" + resource_name + "]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == wanted:
            start = i
            break
    if start is None:
        return False
    end = start + 1
    while end < len(lines) and not (lines[end].strip().startswith("[") and lines[end].strip().endswith("]")):
        end += 1
    del lines[start:end]
    return True


def _apply_object_texture_replacements(ini_text, object_name, mesh_match, group, obj, mod_export_path):
    """把 group 中设定的各贴图槽应用到单个 ini 文本中。

    返回 (新文本, 修改的资源数, 说明)。object_name/mesh_match 用来说明归属；
    group 需含有 _MAP_SLOTS 对应的 4 个 FILE_PATH 属性。
    """
    lines = _split_lines(ini_text)
    object_name = str(object_name or "").strip()
    manual = str(mesh_match or "").strip()

    # 身份候选：由「物体名字符串」+「存在的场景物体(父链)」共同推导，
    # 这样即使 bpy.data.objects 里查不到该对象，也能仅凭名字匹配 [mesh:...]。
    identity_names = set()
    if object_name:
        identity_names |= _identity_candidates_from_name(object_name)
    if obj is not None:
        identity_names |= set(_object_identity_candidates(obj))

    # 1) 定位该物体对应的 TextureOverride 段
    matched_sections = []
    for _section_name, body_indices in _collect_texture_override_sections(lines):
        object_matched = False
        if manual:
            for _index, mesh_name in _iter_mesh_names_in_section(lines, body_indices):
                if mesh_name == manual or manual in mesh_name:
                    object_matched = True
                    break
        else:
            for _index, mesh_name in _iter_mesh_names_in_section(lines, body_indices):
                if _mesh_comment_matches(mesh_name, identity_names):
                    object_matched = True
                    break
        if object_matched:
            matched_sections.append(body_indices)

    if not matched_sections:
        return (
            ini_text,
            0,
            f"'{object_name}'：在该 ini 中未找到对应 [mesh:...] 的贴图引用区块",
        )

    # 2) 依次处理分组内每个已配置的贴图槽
    eol = _detect_eol(ini_text)
    total_modified = 0
    slots_handled = 0
    messages = []

    for attr, label, suffix, param in _MAP_SLOTS:
        source_path_value = str(getattr(group, attr, "") or "").strip()
        if not source_path_value:
            continue
        source_path = os.path.abspath(bpy.path.abspath(source_path_value))
        if not os.path.isfile(source_path):
            messages.append(f"{label}：贴图文件不存在")
            continue

        # 收集该段当前引用的旧资源
        resources = []
        for body_indices in matched_sections:
            resource = _resource_for_map_type(lines, body_indices, suffix)
            if resource and resource not in resources:
                resources.append(resource)
        if not resources:
            messages.append(f"{label}：未找到对应 {suffix} 引用")
            continue

        # 2.1) 复制贴图到 mod/Textures：非 DDS 源（编辑期无损 PNG 等）先按现有规则转 DDS
        texture_dir = os.path.join(mod_export_path, _TEXTURE_FOLDER)
        os.makedirs(texture_dir, exist_ok=True)
        dest_name = os.path.basename(source_path)
        dest_path = os.path.join(texture_dir, dest_name)

        converted = False
        if getattr(group, "export_as_dds", False) and os.path.splitext(dest_name)[1].lower() != ".dds":
            converted_name = os.path.splitext(dest_name)[0] + ".dds"
            converted_path = os.path.join(texture_dir, converted_name)
            # 槽位类型（suffix）是导出侧已知的贴图类型，作为格式推断的兜底提示。
            name_hints = [suffix]
            if os.path.exists(converted_path):
                # 「已存在 DDS」不能盲信：格式与当前规则期望不一致时必须重转。
                expected_format = resolve_texture_dds_format(
                    source_path, None, texture_type=suffix, name_hints=name_hints
                )
                converted = existing_dds_matches_format(converted_path, expected_format)
            if not converted:
                ok, reason = convert_texture_to_dds(
                    source_path,
                    converted_path,
                    texture_type=suffix,
                    name_hints=name_hints,
                )
                if ok and os.path.exists(converted_path):
                    converted = True
                else:
                    messages.append(
                        f"{label}：贴图转 DDS 失败，改用原文件（{reason or '未产出文件'}）"
                    )
            if converted:
                dest_name, dest_path = converted_name, converted_path

        if not converted:
            try:
                if os.path.normcase(os.path.realpath(source_path)) != os.path.normcase(os.path.realpath(dest_path)):
                    if not os.path.exists(dest_path) or os.path.getsize(dest_path) != os.path.getsize(source_path):
                        shutil.copy2(source_path, dest_path)
            except OSError as exc:
                messages.append(f"{label}：复制贴图失败 {exc}")
                continue

        # 2.2) 新建/复用指向该贴图的资源段
        texture_rel = f"{_TEXTURE_FOLDER}/{dest_name}".replace("\\", "/")
        new_resource = _resource_name_for_texture(source_path, label, suffix)
        _ensure_resource_block(lines, new_resource, texture_rel, eol)

        # 2.3) 把每个匹配段里的引用行目标改为新资源
        slot_modified = 0
        for body_indices in matched_sections:
            slot_modified += _replace_assignment_ref(lines, body_indices, param, new_resource, eol)

        # 2.4) 移除不再被任何引用行引用的旧资源段（使其贴图可被清理）
        for resource in resources:
            if resource != new_resource:
                _remove_resource_block_if_unreferenced(lines, resource)

        if slot_modified > 0:
            total_modified += slot_modified
            slots_handled += 1
        else:
            messages.append(f"{label}：已定位区块但未找到 {param} 引用行")

    if total_modified == 0:
        note = f"'{object_name}'：各贴图槽均未改写"
        if messages:
            note += "；" + "；".join(messages)
        return ini_text, 0, note

    return "".join(lines), total_modified, ""


# --------------------------------------------------------------------------- #
# 清理：删除 mod/Textures 下不被任何 ini 引用的文件
# --------------------------------------------------------------------------- #

def _collect_referenced_texture_paths(mod_export_path):
    """收集所有 ini 中 filename 引用的、位于 mod 内 Textures 目录下的相对路径。"""
    referenced = set()
    ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
    for ini_file in ini_files:
        try:
            content = _read_text(ini_file)
        except OSError:
            continue
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith("filename"):
                continue
            match = _FILENAME_RE.match(stripped)
            if not match:
                continue
            value = match.group("value").strip()
            resolved = _resolve_inside(mod_export_path, value)
            if resolved is None:
                continue
            rel = os.path.relpath(resolved, mod_export_path).replace("\\", "/")
            rel_lower = rel.lower()
            if not rel_lower.startswith(_TEXTURE_FOLDER.lower()):
                continue
            referenced.add(rel_lower)
    return referenced


def _cleanup_unreferenced_textures(mod_export_path):
    """删除 mod/Textures 下不被任何 ini 引用的文件，返回删除数量。"""
    texture_dir = os.path.join(mod_export_path, _TEXTURE_FOLDER)
    if not os.path.isdir(texture_dir):
        return 0
    referenced = _collect_referenced_texture_paths(mod_export_path)
    removed = 0
    for folder, _dir_names, file_names in os.walk(texture_dir):
        for file_name in file_names:
            file_path = os.path.join(folder, file_name)
            rel = os.path.relpath(file_path, mod_export_path).replace("\\", "/").lower()
            if rel in referenced:
                continue
            try:
                os.remove(file_path)
                removed += 1
                print(f"[{NODE_LABEL}] 已删除未引用贴图: {rel}")
            except OSError as exc:
                print(f"[{NODE_LABEL}] 删除失败 {file_path}: {exc}")
    return removed


# --------------------------------------------------------------------------- #
# 蓝图树物体检索
# --------------------------------------------------------------------------- #

_OUTPUT_IDS = {
    "SSMTNode_Result_Output",
    "SSMTNode_Result_Output_NTMIModImp",
    "SSMTNode_VeloExportBridge",
}
_SOURCE_IDS = {"SSMTNode_Object_Info", "SSMTNode_MultiFile_Export"}


def _tree_unique_key(node):
    tree = getattr(node, "id_data", None)
    tree_name = getattr(tree, "name", "") if tree else ""
    return f"{tree_name}::{node.name}"


def _find_connected_result_output(node, visited=None):
    if visited is None:
        visited = set()
    key = _tree_unique_key(node)
    if key in visited:
        return None
    visited.add(key)
    if getattr(node, "bl_idname", "") in _OUTPUT_IDS:
        return node
    for input_socket in node.inputs:
        if getattr(input_socket, "bl_idname", "") != "SSMTSocketPostProcess":
            continue
        if not input_socket.is_linked:
            continue
        for link in input_socket.links:
            found = _find_connected_result_output(link.from_node, visited)
            if found is not None:
                return found
    return None


def _collect_source_object_names_from_output(output_node, visited=None, visited_trees=None):
    """从输出节点回溯，收集所有物体来源节点上引用的物体名（含嵌套蓝图）。"""
    if visited is None:
        visited = set()
    if visited_trees is None:
        visited_trees = set()

    names = []

    def walk(node):
        key = _tree_unique_key(node)
        if key in visited:
            return
        visited.add(key)

        bl_idname = getattr(node, "bl_idname", "")
        if bl_idname in _SOURCE_IDS:
            if bl_idname == "SSMTNode_Object_Info":
                obj_name = getattr(node, "object_name", "")
                if obj_name:
                    names.append(obj_name)
            elif bl_idname == "SSMTNode_MultiFile_Export":
                for item in getattr(node, "object_list", []):
                    obj_name = getattr(item, "object_name", "")
                    if obj_name:
                        names.append(obj_name)
            return

        if bl_idname == "SSMTNode_Blueprint_Nest":
            nested_name = str(getattr(node, "blueprint_name", "") or "").strip()
            if nested_name and nested_name != "NONE":
                nested = bpy.data.node_groups.get(nested_name)
                if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                    if nested_name not in visited_trees:
                        visited_trees.add(nested_name)
                        for nested_node in nested.nodes:
                            if getattr(nested_node, "bl_idname", "") in _OUTPUT_IDS:
                                walk(nested_node)

        for input_socket in node.inputs:
            if not input_socket.is_linked:
                continue
            for link in input_socket.links:
                walk(link.from_node)

    walk(output_node)
    return names


# --------------------------------------------------------------------------- #
# PropertyGroup：分组设置 与 物体条目
# --------------------------------------------------------------------------- #

class SSMT_ObjectTextureItem(bpy.types.PropertyGroup):
    """物体条目（全局列表）；group_index=0 表示未分配。"""
    object_name: bpy.props.StringProperty(name="物体", default="")
    mesh_match: bpy.props.StringProperty(
        name="INI 网格名(可选)",
        description="留空自动按物体在蓝图/场景中的完整路径匹配；"
                    "填了则按此字符串去匹配 INI 中的 [mesh:...] 注释（含/包含均可）",
        default="",
    )
    group_index: bpy.props.IntProperty(name="分组编号", default=0, min=0)
    status: bpy.props.StringProperty(name="状态", default="")


class SSMT_ObjectTextureGroup(bpy.types.PropertyGroup):
    """一个贴图分组：该组内所有物体共用下面的 4 个贴图槽。"""
    group_index: bpy.props.IntProperty(name="分组编号", default=1, min=1)
    remark: bpy.props.StringProperty(name="分组备注", default="")
    expanded: bpy.props.BoolProperty(name="展开", default=True)
    export_as_dds: bpy.props.BoolProperty(
        name="导出时把贴图转成DDS",
        description=(
            "导出 Mod 时，本组贴图若不是 .dds（例如编辑期用的无损 PNG），"
            "先用 texconv 按现有 DDS 转换规则转成 DDS 放进 Mod 的 Textures 目录，"
            "INI 的 filename 也指向转出来的 DDS。源贴图文件保留不动。"
            "关闭时按原样复制源文件"
        ),
        default=True,
    )
    diffuse_path: bpy.props.StringProperty(
        name="Diffuse 贴图",
        description="该组物体的 Diffuse 贴图，导出时复制进 mod/Textures 并改写对应 DiffuseMap 引用",
        subtype="FILE_PATH",
        default="",
    )
    normal_path: bpy.props.StringProperty(
        name="Normal 贴图",
        description="该组物体的 Normal 贴图，导出时复制进 mod/Textures 并改写对应 NormalMap 引用",
        subtype="FILE_PATH",
        default="",
    )
    light_path: bpy.props.StringProperty(
        name="Light 贴图",
        description="该组物体的 Light 贴图，导出时复制进 mod/Textures 并改写对应 LightMap 引用",
        subtype="FILE_PATH",
        default="",
    )
    material_path: bpy.props.StringProperty(
        name="Material 贴图",
        description="该组物体的 Material 贴图，导出时复制进 mod/Textures 并改写对应 MaterialMap 引用",
        subtype="FILE_PATH",
        default="",
    )


# --------------------------------------------------------------------------- #
# 操作符
# --------------------------------------------------------------------------- #

def _find_node(context, node_name):
    space = getattr(context, "space_data", None)
    if not space or space.type != "NODE_EDITOR":
        return None
    tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
    node = tree.nodes.get(node_name) if tree else None
    return node if node and node.bl_idname == NODE_IDNAME else None


class SSMT_OT_ObjectTextureScanTree(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_scan_tree"
    bl_label = "刷新物体"
    bl_description = "检索当前蓝图节点树（含嵌套）上引用的网格物体并加入列表（保留已有分组归属）"

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}

        output = _find_connected_result_output(node)
        if output is None:
            self.report({"WARNING"}, "未找到与输出节点相连的后处理链，请先把本节点连接到输出后处理链")
            return {"CANCELLED"}

        obj_names = _collect_source_object_names_from_output(output)
        if not obj_names:
            self.report({"WARNING"}, "在后处理链的物体来源节点中未找到任何物体")
            return {"CANCELLED"}

        existing = {entry.object_name: entry for entry in node.object_entries}
        added = 0
        seen = set()
        for obj_name in obj_names:
            if obj_name in seen:
                continue
            seen.add(obj_name)
            obj = bpy.data.objects.get(obj_name)
            if obj is None or obj.type != "MESH":
                continue
            if obj_name in existing:
                continue
            entry = node.object_entries.add()
            entry.object_name = obj_name
            entry.group_index = 0
            added += 1

        self.report({"INFO"}, f"扫描完成：共 {len(node.object_entries)} 个物体，本次新增 {added} 个")
        return {"FINISHED"}


class SSMT_OT_ObjectTextureAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前视图中选中的网格物体加入列表（未分配）"

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        existing = {entry.object_name for entry in node.object_entries}
        added = 0
        for obj in context.selected_objects or []:
            if obj.type != "MESH":
                continue
            if obj.name in existing:
                continue
            entry = node.object_entries.add()
            entry.object_name = obj.name
            entry.group_index = 0
            added += 1
            existing.add(obj.name)
        self.report({"INFO"}, f"新增 {added} 个选中物体")
        return {"FINISHED"}


class SSMT_OT_ObjectTextureEntryRemove(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_entry_remove"
    bl_label = "删除条目"
    node_name: bpy.props.StringProperty()
    object_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        for index, entry in enumerate(node.object_entries):
            if entry.object_name == self.object_name:
                node.object_entries.remove(index)
                break
        return {"FINISHED"}


class SSMT_OT_ObjectTextureGroupAdd(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_group_add"
    bl_label = "创建新分组"
    bl_options = {"REGISTER", "INTERNAL"}
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        max_idx = 0
        for setting in node.texture_groups:
            if setting.group_index > max_idx:
                max_idx = setting.group_index
        new_group = node.texture_groups.add()
        new_group.group_index = max_idx + 1
        node.active_group_index = new_group.group_index
        self.report({"INFO"}, f"已创建并切换至分组 {new_group.group_index}")
        return {"FINISHED"}


class SSMT_OT_ObjectTextureGroupRemove(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_group_remove"
    bl_label = "删除分组"
    bl_options = {"REGISTER", "INTERNAL"}
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        if len(node.texture_groups) <= 1:
            self.report({"WARNING"}, "至少保留一个分组，不能删除最后一个分组")
            return {"CANCELLED"}

        for entry in node.object_entries:
            if entry.group_index == self.target_group_index:
                entry.group_index = 0

        idx_to_remove = -1
        for i, setting in enumerate(node.texture_groups):
            if setting.group_index == self.target_group_index:
                idx_to_remove = i
                break
        if idx_to_remove != -1:
            node.texture_groups.remove(idx_to_remove)

        if node.active_group_index == self.target_group_index:
            if len(node.texture_groups) > 0:
                node.active_group_index = node.texture_groups[0].group_index

        self.report({"INFO"}, f"已删除分组 {self.target_group_index}，相关物体已移至未分配")
        return {"FINISHED"}


class SSMT_OT_ObjectTextureGroupMove(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_group_move"
    bl_label = "移动分组"
    bl_options = {"REGISTER", "INTERNAL"}
    node_name: bpy.props.StringProperty()
    direction: bpy.props.EnumProperty(items=[("UP", "上移", ""), ("DOWN", "下移", "")])
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        settings = node.texture_groups
        idx = -1
        for i, setting in enumerate(settings):
            if setting.group_index == self.target_group_index:
                idx = i
                break
        if idx == -1:
            return {"CANCELLED"}
        new_idx = idx + (1 if self.direction == "DOWN" else -1)
        if new_idx < 0 or new_idx >= len(settings):
            return {"CANCELLED"}

        settings.move(idx, new_idx)

        index_map = {}
        for i, setting in enumerate(settings):
            old_index = setting.group_index
            setting.group_index = i + 1
            index_map[old_index] = i + 1

        for entry in node.object_entries:
            if entry.group_index in index_map:
                entry.group_index = index_map[entry.group_index]

        if node.active_group_index in index_map:
            node.active_group_index = index_map[node.active_group_index]

        return {"FINISHED"}


class SSMT_OT_ObjectTextureSelectGroup(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_select_group"
    bl_label = "切换到此分组"
    node_name: bpy.props.StringProperty()
    target_group_index: bpy.props.IntProperty(default=1)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        node.active_group_index = self.target_group_index
        return {"FINISHED"}


class SSMT_OT_ObjectTextureAssignCurrentGroup(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_assign_current"
    bl_label = "移入当前分组"
    bl_description = "把该物体加入当前选中分组"
    node_name: bpy.props.StringProperty()
    object_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        for entry in node.object_entries:
            if entry.object_name == self.object_name:
                entry.group_index = node.active_group_index
                break
        return {"FINISHED"}


class SSMT_OT_ObjectTextureUnassignGroup(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_unassign"
    bl_label = "移出分组"
    bl_description = "把该物体移回未分配"
    node_name: bpy.props.StringProperty()
    object_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        for entry in node.object_entries:
            if entry.object_name == self.object_name:
                entry.group_index = 0
                break
        return {"FINISHED"}


class SSMT_OT_ObjectTextureApply(bpy.types.Operator):
    bl_idname = "ssmt.object_texture_apply"
    bl_label = "立即应用/更新"
    bl_description = "按当前分组设置立即改写目标 mod ini 的贴图引用并复制贴图/清理未引用"
    bl_options = {"REGISTER", "INTERNAL"}
    node_name: bpy.props.StringProperty()
    cleanup_only: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        ini_path = str(getattr(node, "ini_file_path", "") or "").strip()
        if not ini_path:
            self.report({"ERROR"}, "请先在节点上选择目标 mod ini 路径")
            return {"CANCELLED"}
        abs_path = os.path.abspath(bpy.path.abspath(ini_path))
        if not os.path.isfile(abs_path):
            self.report({"ERROR"}, f"ini 文件不存在：{abs_path}")
            return {"CANCELLED"}
        root = os.path.dirname(abs_path)

        if self.cleanup_only:
            removed = _cleanup_unreferenced_textures(root)
            self.report({"INFO"}, f"清理完成：删除未引用贴图 {removed} 个")
            return {"FINISHED"}

        result = node._run_processing(root)
        self.report(
            {"INFO"},
            f"应用完成：改写 {result['modified']} 处，处理 {result['handled']} 个物体/ini，"
            f"删除未引用贴图 {result['removed']} 个",
        )
        if result["warnings"]:
            self.report({"WARNING"}, "；".join(result["warnings"][:3]))
        return {"FINISHED"}


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #

class SSMTNode_PostProcess_ObjectTextureAssign(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = NODE_LABEL
    bl_description = (
        "按分组为物体指定 Diffuse/Normal/Light/Material 贴图；导出时按 [mesh:...] "
        "定位 ini 中对应贴图引用并改写其地图定义，把贴图复制进 mod 的 "
        "Textures，并删除不被任何 ini 引用的其它贴图文件"
    )

    object_entries: bpy.props.CollectionProperty(type=SSMT_ObjectTextureItem)
    texture_groups: bpy.props.CollectionProperty(type=SSMT_ObjectTextureGroup)
    active_group_index: bpy.props.IntProperty(default=1)
    show_unassigned: bpy.props.BoolProperty(name="未分配", default=True)
    clean_unreferenced: bpy.props.BoolProperty(
        name="删除未引用贴图",
        description="处理完成后删除 mod/Textures 下不被任何 ini 引用的贴图文件",
        default=True,
    )
    create_backup: bpy.props.BoolProperty(
        name="自动备份 ini",
        description="改写 ini 前在 mod/Backups 生成时间戳备份",
        default=True,
    )
    ini_file_path: bpy.props.StringProperty(
        name="目标 mod ini",
        description="用于『立即应用/更新』时定位要处理的 mod ini（其所在目录即为 mod 根目录，含 Textures 子目录）",
        subtype="FILE_PATH",
        default="",
    )

    def init(self, context):
        super().init(context)
        self.width = 560
        if not self.texture_groups:
            new_group = self.texture_groups.add()
            new_group.group_index = 1

    def _find_group_setting(self, group_index):
        for setting in self.texture_groups:
            if setting.group_index == group_index:
                return setting
        return None

    def _iter_ini_paths(self, mod_export_path):
        return sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))

    def draw_buttons(self, context, layout):
        header = layout.box()
        header.label(text="按分组为物体指定 Diffuse/Normal/Light/Material 贴图", icon="TEXTURE")
        header.label(text="导出时改写对应贴图引用并清理未引用贴图（不改动既有节点）", icon="INFO")

        scan_row = layout.row(align=True)
        op = scan_row.operator("ssmt.object_texture_scan_tree", text="刷新物体", icon="FILE_REFRESH")
        op.node_name = self.name
        op = scan_row.operator("ssmt.object_texture_add_selected", text="添加选中物体", icon="OBJECT_DATA")
        op.node_name = self.name

        options = layout.row(align=True)
        options.prop(self, "create_backup")
        options.prop(self, "clean_unreferenced")
        layout.separator()

        apply_box = layout.box()
        apply_box.label(text="即时应用/更新（无需重新导出，便于验证）", icon='FILE_TICK')
        apply_box.prop(self, "ini_file_path", text="目标 mod ini")
        apply_row = apply_box.row(align=True)
        apply_row.operator("ssmt.object_texture_apply", text="立即应用/更新", icon='CHECKMARK').node_name = self.name
        apply_clean = apply_row.operator("ssmt.object_texture_apply", text="仅清理未引用贴图", icon='TRASH')
        apply_clean.node_name = self.name
        apply_clean.cleanup_only = True
        layout.separator()

        box = layout.box()
        box.label(text="贴图分组与物体管理", icon='SHAPEKEY_DATA')
        row = box.row(align=True)
        row.operator("ssmt.object_texture_group_add", text="创建新分组", icon='ADD').node_name = self.name

        for setting in self.texture_groups:
            group_container = box.box()

            row = group_container.row(align=True)
            row.prop(setting, "expanded", text="",
                     icon='TRIA_DOWN' if setting.expanded else 'TRIA_RIGHT', emboss=False)

            is_active = (setting.group_index == self.active_group_index)
            act_op = row.operator("ssmt.object_texture_select_group", text="",
                                  icon='CHECKBOX_HLT' if is_active else 'CHECKBOX_DEHLT', emboss=False)
            act_op.node_name = self.name
            act_op.target_group_index = setting.group_index

            sw_op = row.operator(
                "ssmt.object_texture_select_group",
                text=f"分组 {setting.group_index} - {setting.remark or '未命名'}",
                icon='GROUP',
            )
            sw_op.node_name = self.name
            sw_op.target_group_index = setting.group_index

            row.separator()
            op_up = row.operator("ssmt.object_texture_group_move", text="", icon='TRIA_UP')
            op_up.node_name = self.name
            op_up.direction = 'UP'
            op_up.target_group_index = setting.group_index

            op_down = row.operator("ssmt.object_texture_group_move", text="", icon='TRIA_DOWN')
            op_down.node_name = self.name
            op_down.direction = 'DOWN'
            op_down.target_group_index = setting.group_index

            op_del = row.operator("ssmt.object_texture_group_remove", text="", icon='X')
            op_del.node_name = self.name
            op_del.target_group_index = setting.group_index

            if not setting.expanded:
                continue

            # 分组贴图设置（组内物体共用）
            set_box = group_container.box()
            set_box.label(text="贴图设置（组内物体共用）", icon='IMAGE_DATA')
            for _attr, label, _suffix, _param in _MAP_SLOTS:
                slot_row = set_box.row(align=True)
                slot_row.label(text=f"{label}:", icon='IMAGE_DATA')
                slot_row.prop(setting, _attr, text="")
            set_box.prop(setting, "remark", text="分组备注")
            set_box.prop(setting, "export_as_dds")

            # 组内物体列表
            members = [entry for entry in self.object_entries if entry.group_index == setting.group_index]
            child = group_container.box()
            child.label(text=f"组内物体 ({len(members)})", icon='OBJECT_DATA')
            if not members:
                child.label(text="     (暂无物体)", icon='INFO')
            for entry in members:
                item_row = child.row(align=True)
                item_row.label(text=entry.object_name, icon='OUTLINER_OB_MESH')
                op_out = item_row.operator("ssmt.object_texture_unassign", text="移出分组", icon='X')
                op_out.node_name = self.name
                op_out.object_name = entry.object_name

        # 未分配区域
        box = layout.box()
        row = box.row(align=True)
        row.prop(self, "show_unassigned", text="",
                 icon='TRIA_DOWN' if self.show_unassigned else 'TRIA_RIGHT', emboss=False)
        row.label(text="未分配物体", icon='PARTICLES')

        if self.show_unassigned:
            sub_box = box.box()
            unassigned = [entry for entry in self.object_entries if entry.group_index == 0]
            if not unassigned:
                sub_box.label(text="     (暂无未分配的物体)", icon='INFO')
            for entry in unassigned:
                item_row = sub_box.row(align=True)
                item_row.label(text=entry.object_name, icon='OUTLINER_OB_MESH')
                op_add = item_row.operator("ssmt.object_texture_assign_current", text="移入当前分组", icon='ADD')
                op_add.node_name = self.name
                op_add.object_name = entry.object_name
                op_del = item_row.operator("ssmt.object_texture_entry_remove", text="", icon='X')
                op_del.node_name = self.name
                op_del.object_name = entry.object_name

    def _run_processing(self, mod_export_path):
        print(f"[{NODE_LABEL}] 开始执行，输出路径: {mod_export_path}")
        ini_files = self._iter_ini_paths(mod_export_path)
        if not ini_files:
            print(f"[{NODE_LABEL}] 未找到生成的 .ini 文件，跳过")
            return {"modified": 0, "handled": 0, "removed": 0, "warnings": ["未找到 ini 文件"]}

        enabled_entries = [entry for entry in self.object_entries if entry.group_index > 0 and entry.object_name]
        if not enabled_entries:
            print(f"[{NODE_LABEL}] 未分配任何物体到分组，跳过")
            return {"modified": 0, "handled": 0, "removed": 0, "warnings": ["未分配任何物体到分组"]}

        # 相同 LOD 前缀的物体按「同一个物体」处理：同一前缀只保留一个代表条目，
        # 避免重复改写同一资源、也避免同前缀物体被分到不同分组时互相冲突。
        deduped_entries = []
        seen_keys = set()
        for entry in enabled_entries:
            key = _entry_match_key(entry)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_entries.append(entry)
        enabled_entries = deduped_entries
        if not enabled_entries:
            print(f"[{NODE_LABEL}] 没有可处理的目标物体，跳过")
            return {"modified": 0, "handled": 0, "removed": 0, "warnings": ["没有可处理的目标物体"]}

        object_cache = {}
        group_cache = {}
        for entry in enabled_entries:
            object_cache[entry.object_name] = bpy.data.objects.get(entry.object_name)
            group_cache[entry.object_name] = self._find_group_setting(entry.group_index)

        total_modified_resources = 0
        total_items_handled = 0
        warnings = []

        for ini_file in ini_files:
            if self.create_backup:
                self._create_cumulative_backup(ini_file, mod_export_path)
            try:
                ini_text = _read_text(ini_file)
            except OSError as exc:
                warnings.append(f"读取 ini 失败 {ini_file}: {exc}")
                continue

            changed_this_ini = False
            for entry in enabled_entries:
                group = group_cache.get(entry.object_name)
                if group is None:
                    warnings.append(f"[{entry.object_name}] 所属分组 {entry.group_index} 不存在，跳过")
                    continue
                obj = object_cache.get(entry.object_name)
                new_text, modified, message = _apply_object_texture_replacements(
                    ini_text,
                    entry.object_name,
                    entry.mesh_match,
                    group,
                    obj,
                    mod_export_path,
                )
                if message and modified == 0:
                    warnings.append(f"[{entry.object_name}] {message}")
                elif modified > 0:
                    entry.status = f"已改写 {modified} 处"
                    changed_this_ini = True
                    total_modified_resources += modified
                    total_items_handled += 1
                ini_text = new_text

            if changed_this_ini:
                _write_text(ini_file, ini_text)
                print(f"[{NODE_LABEL}] 已改写: {os.path.basename(ini_file)}")

        removed_count = 0
        if self.clean_unreferenced:
            removed_count = _cleanup_unreferenced_textures(mod_export_path)

        print(
            f"[{NODE_LABEL}] 完成：改写 {total_modified_resources} 处地图定义，"
            f"处理 {total_items_handled} 个物体/ini，删除未引用贴图 {removed_count} 个"
        )
        for warning in warnings:
            print(f"[{NODE_LABEL}] 提示: {warning}")
        return {
            "modified": total_modified_resources,
            "handled": total_items_handled,
            "removed": removed_count,
            "warnings": warnings,
        }

    def execute_postprocess(self, mod_export_path):
        result = self._run_processing(mod_export_path)
        return bool(result["modified"] or result["handled"])


classes = (
    SSMT_ObjectTextureItem,
    SSMT_ObjectTextureGroup,
    SSMT_OT_ObjectTextureScanTree,
    SSMT_OT_ObjectTextureAddSelected,
    SSMT_OT_ObjectTextureEntryRemove,
    SSMT_OT_ObjectTextureGroupAdd,
    SSMT_OT_ObjectTextureGroupRemove,
    SSMT_OT_ObjectTextureGroupMove,
    SSMT_OT_ObjectTextureSelectGroup,
    SSMT_OT_ObjectTextureAssignCurrentGroup,
    SSMT_OT_ObjectTextureUnassignGroup,
    SSMT_OT_ObjectTextureApply,
    SSMTNode_PostProcess_ObjectTextureAssign,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
