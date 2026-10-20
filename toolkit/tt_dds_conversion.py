import json
import os
import re
import shutil
import struct
import subprocess
import tempfile

import bpy

TOOLSET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "Toolset")

DDS_DEFAULT_RULES = [
    {
        "texture_type": "DiffuseMap",
        "pattern": r"(?i)(?:^|[_\-. ])DiffuseMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "NormalMap",
        "pattern": r"(?i)(?:^|[_\-. ])NormalMap(?:[_\-. ]|$)",
        "format": "r8g8b8a8_unorm",
    },
    {
        "texture_type": "LightMap",
        "pattern": r"(?i)(?:^|[_\-. ])LightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "MaterialMap",
        "pattern": r"(?i)(?:^|[_\-. ])MaterialMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "RampMap",
        "pattern": r"(?i)(?:^|[_\-. ])RampMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "HighLightMap",
        "pattern": r"(?i)(?:^|[_\-. ])HighLightMap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "WengineFX",
        "pattern": r"(?i)(?:^|[_\-. ])WengineFx(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "Glowmap",
        "pattern": r"(?i)(?:^|[_\-. ])Glowmap(?:[_\-. ]|$)",
        "format": "bc7_unorm_srgb",
    },
    {
        "texture_type": "FXMap",
        "pattern": r"(?i)(?:^|[_\-. ])FXMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "TTLMap",
        "pattern": r"(?i)(?:^|[_\-. ])TTLMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "RoughnessMap",
        "pattern": r"(?i)(?:^|[_\-. ])RoughnessMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
    {
        "texture_type": "ORMMap",
        "pattern": r"(?i)(?:^|[_\-. ])ORMMap(?:[_\-. ]|$)",
        "format": "bc7_unorm",
    },
]

_SUPPORTED_SOURCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}

# 目标格式名 -> DX10 头里偏移 128 的 DXGI 格式号（大小写不敏感比较）
_DDS_DXGI_FORMAT_IDS = {
    "bc7_unorm": 98,
    "bc7_unorm_srgb": 99,
    "bc6h_uf16": 95,
    "bc6h_sf16": 96,
    "bc1_unorm": 71,
    "bc1_unorm_srgb": 72,
    "bc2_unorm": 74,
    "bc2_unorm_srgb": 75,
    "bc3_unorm": 77,
    "bc3_unorm_srgb": 78,
    "bc4_unorm": 80,
    "bc5_unorm": 83,
    "r8g8b8a8_unorm": 28,
    "r8g8b8a8_unorm_srgb": 29,
    "b8g8r8a8_unorm": 87,
    "b8g8r8a8_unorm_srgb": 91,
}

# legacy 头（FOURCC 在偏移 84）-> 等价的非 sRGB 格式名。
# legacy FOURCC 没有 sRGB 变体，所以它们永远匹配不上任何 _srgb 目标。
_DDS_LEGACY_FOURCC_FORMATS = {
    b"DXT1": "bc1_unorm",
    b"DXT2": "bc2_unorm",
    b"DXT3": "bc2_unorm",
    b"DXT4": "bc3_unorm",
    b"DXT5": "bc3_unorm",
    b"ATI1": "bc4_unorm",
    b"BC4U": "bc4_unorm",
    b"ATI2": "bc5_unorm",
    b"BC5U": "bc5_unorm",
}

# DDS magic(4) + DDS_HEADER(124) + DX10 扩展头(20)
_DDS_FULL_HEADER_SIZE = 148


def find_texconv():
    props = bpy.context.scene.texture_tools_props

    local_path = os.path.join(TOOLSET_PATH, "texconv.exe")
    if os.path.exists(local_path):
        return local_path

    if props and props.texconv_path and os.path.exists(props.texconv_path):
        return props.texconv_path

    system_path = shutil.which("texconv")
    if system_path:
        return system_path

    return None


def _get_match_targets(filename: str) -> list[str]:
    basename = os.path.basename(filename or "")
    stem, _ext = os.path.splitext(basename)
    targets = []
    for value in (stem, basename):
        value = str(value or "").strip()
        if value and value not in targets:
            targets.append(value)
    return targets


def _pattern_matches(pattern: str, filename: str) -> bool:
    targets = _get_match_targets(filename)
    return any(re.search(pattern, target) for target in targets)


def _validate_custom_rules(props) -> list[str]:
    errors = []
    if not getattr(props, "dds_use_custom_rules", False):
        return errors

    for index, rule in enumerate(getattr(props, "dds_rules", []) or [], start=1):
        if not getattr(rule, "enabled", True):
            continue
        pattern = str(getattr(rule, "pattern", "") or "").strip()
        if not pattern:
            errors.append(f"规则 {index} 缺少正则表达式")
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"规则 {index} 正则无效: {exc}")
    return errors


def _format_is_srgb(dds_format: str) -> bool:
    return "_srgb" in str(dds_format or "").strip().lower()


def _texconv_colorspace_flags(dds_format: str) -> list[str]:
    """按输出 DXGI 格式决定 texconv 的色彩空间标志，保证「数值不变、颜色不变」。

    规则：**输出格式与输入格式的 sRGB 属性一致时才恒等**，不一致就会产生一次色彩变换。
    - ``_srgb`` 输出（bc7_unorm_srgb 等）：输入通常是 PNG 这类**没有色彩空间信息**的
      WIC 位图（texconv 默认当线性读），此时必须用 ``--srgb-in`` 让它按 sRGB 解码、
      再由 sRGB 输出编码回去，净效果恒等。
    - 非 sRGB 输出（bc7_unorm、r8g8b8a8_unorm 等）：用 ``--ignore-srgb`` 按原始数值读写。

    **重要边界（2026-09-27 实测澄清）**：``--ignore-srgb`` 只作用于 WIC 输入/输出，
    **管不了 DDS 自身的 ``_SRGB`` 格式语义**——texconv 读 ``BC7_UNORM_SRGB`` 一定解码、
    写它一定编码。因此把「已经是 _SRGB 的 DDS」按非 sRGB 规则重编码，会净压暗一次
    （反向则提亮一次）；用户工作空间里「颜色贴图整体变暗」正是这么来的
    （规则表被改成全 ``bc7_unorm``，而磁盘上的贴图是 ``_SRGB``）。
    要避免它：让规则与现网文件的 sRGB 属性保持一致，而不是指望开关去补偿。"""
    if _format_is_srgb(dds_format):
        return ["--srgb-in"]
    return ["--ignore-srgb"]


def _apply_image_colorspace(image, dds_format: str):
    """按输出格式设置 Blender 显示色彩空间。

    **注意：这已经不是重连路径的常规动作**，只在快照取不到有效值时作为兜底调用
    （见 :func:`_restore_image_display_state`）。b7b1992 曾把「Blender 显示色彩空间
    按输出格式的 _srgb 后缀设置」当成重连后的常规动作，本次（用户 2026-09-27 要求）
    推翻该行为：重连只换容器格式与文件路径，必须沿用原本的纹理节点配置，不得改写
    用户已设好的色彩空间。
    """
    try:
        image.colorspace_settings.name = "sRGB" if _format_is_srgb(dds_format) else "Non-Color"
    except Exception:
        pass


def _snapshot_image_display_state(image) -> dict:
    """重连前记录图片自身的显示配置（色彩空间 / Alpha 模式）。

    为什么只快照这两个字段：`filepath` + `reload()` 只重建 ImBuf，实测（Blender 5.0.1，
    见 .dbg/relink_probe/relink_probe_result.json）不会改动引用该图的
    `ShaderNodeTexImage` 节点的 interpolation / extension / projection，也不动
    `image_user`；本算子同样不写任何节点字段。因此会覆盖用户配置的只有「按格式硬设
    色彩空间」这一条，快照这两个字段即可覆盖全部会被改写的入口。

    取不到值的字段留空串，由恢复侧自行决定是否按格式兜底。
    """
    state = {"colorspace": "", "alpha_mode": ""}
    try:
        state["colorspace"] = str(image.colorspace_settings.name or "")
    except Exception:
        pass
    try:
        state["alpha_mode"] = str(image.alpha_mode or "")
    except Exception:
        pass
    return state


def _restore_image_display_state(image, snapshot, dds_format: str) -> None:
    """重连后把快照原样写回：转换只换容器格式与文件路径，不改用户既有配置。

    只有快照取不到有效值（读取异常 / 空串）或写回失败时，才退回「按输出格式设置
    色彩空间」的兜底；所有异常都在内部吃掉，绝不中断整个转换循环。
    """
    colorspace = str((snapshot or {}).get("colorspace") or "")
    if not colorspace:
        _apply_image_colorspace(image, dds_format)
    else:
        try:
            if image.colorspace_settings.name != colorspace:
                image.colorspace_settings.name = colorspace
        except Exception:
            _apply_image_colorspace(image, dds_format)

    alpha_mode = str((snapshot or {}).get("alpha_mode") or "")
    if alpha_mode:
        try:
            if image.alpha_mode != alpha_mode:
                image.alpha_mode = alpha_mode
        except Exception:
            pass


_TEXTURE_TYPE_SPLIT_RE = re.compile(r"[_.\-\s]+")


def _first_texture_type_token(texture_type: str) -> str:
    """取类型名的首个 token（按 ``_`` ``.`` ``-`` 空格切分），无有效 token 返回空串。"""
    for token in _TEXTURE_TYPE_SPLIT_RE.split(str(texture_type or "").strip()):
        if token:
            return token
    return ""


def format_for_texture_type(texture_type: str, props) -> str:
    """把材质类型 / 材质名前缀解析成 DDS 格式；未命中返回空串由调用方回退。

    输入可以是 ``DiffuseMap``、``MaterialMap``、``diffusemap_xxx``、
    ``DiffuseMap_VivianGemADiffuse`` 等：取首个 token 判定，大小写不敏感。
    解析顺序：自定义规则（pattern 命中完整类型名）→ 默认规则的 ``texture_type``。
    """
    raw = str(texture_type or "").strip()
    if not raw:
        return ""

    if getattr(props, "dds_use_custom_rules", False):
        for rule in getattr(props, "dds_rules", []) or []:
            if not getattr(rule, "enabled", True):
                continue
            pattern = str(getattr(rule, "pattern", "") or "").strip()
            if not pattern:
                continue
            try:
                if _pattern_matches(pattern, raw):
                    rule_format = str(getattr(rule, "format", "") or "").strip()
                    if rule_format:
                        return rule_format
            except re.error:
                continue

    key = _first_texture_type_token(raw).lower()
    if not key:
        return ""
    for rule in DDS_DEFAULT_RULES:
        if str(rule.get("texture_type", "") or "").strip().lower() == key:
            return str(rule.get("format", "") or "")
    return ""


def _resolve_single_name(name: str, props):
    """按单个候选名匹配规则；未命中返回 None。

    自定义规则优先、默认规则取最早 match 位置 —— 与原 ``resolve_dds_target``
    的匹配语义完全一致，只是抽出来供「源文件名 / hints」逐个重试。
    """
    candidate = str(name or "").strip()
    if not candidate:
        return None

    if getattr(props, "dds_use_custom_rules", False):
        for rule in getattr(props, "dds_rules", []) or []:
            if not getattr(rule, "enabled", True):
                continue
            pattern = str(getattr(rule, "pattern", "") or "").strip()
            if not pattern:
                continue
            try:
                if _pattern_matches(pattern, candidate):
                    rule_format = str(getattr(rule, "format", "") or "").strip()
                    # 自定义规则完全由用户说了算：pattern 只负责识别名称，
                    # format 决定输出格式，不再从文件名推断类型做色彩空间干预
                    return "custom", rule_format or "bc7_unorm", pattern
            except re.error:
                continue

    targets = _get_match_targets(candidate)
    best_rule = None
    best_start = float("inf")
    for rule in DDS_DEFAULT_RULES:
        try:
            pattern = rule["pattern"]
            for target in targets:
                match = re.search(pattern, target)
                if match:
                    if match.start() < best_start:
                        best_start = match.start()
                        best_rule = rule
                    break
        except re.error:
            continue

    if best_rule is not None:
        return best_rule["texture_type"], best_rule["format"], best_rule["texture_type"]
    return None


def resolve_dds_target(filename: str, props, name_hints=()) -> tuple[str, str, str]:
    """按「源文件名 → name_hints」的顺序推断 DDS 目标格式。

    源文件名匹配保持原有语义；源文件名**一个规则都没命中**时，才依次用
    ``name_hints``（材质名 / 贴图槽类型标签 / 输出文件名 stem 等）重试，
    第一个命中的候选名直接返回。全不命中仍回退 ``default/bc7_unorm``。

    为什么要 hints：Mod 导出时输出名已被改成材质资源名，编辑期的源 PNG 可能
    只含 ``Diffuse`` 而不含 ``DiffuseMap``（实测 ``VivianGemADiffuse.png``），
    只按源文件名匹配就会落到线性默认规则。
    """
    hit = _resolve_single_name(filename, props)
    if hit is not None:
        return hit

    for hint in name_hints or ():
        hit = _resolve_single_name(hint, props)
        if hit is not None:
            return hit

    return "default", "bc7_unorm", "Default"


def resolve_texture_dds_format(
    source_path: str,
    props,
    texture_type: str = "",
    name_hints=(),
    format_override: str = "",
) -> str:
    """单文件转换的最终输出格式。

    优先级：``format_override``（非空）→ ``format_for_texture_type(texture_type)``
    → ``resolve_dds_target(源文件名, name_hints)`` 的最后兜底。导出侧有材质类型
    信息时必须传 ``texture_type``，不能再让源文件名决定格式（实测
    ``VivianGemADiffuse.png`` 不含 ``DiffuseMap`` 关键字会落到线性默认规则）。
    """
    override = str(format_override or "").strip()
    if override:
        return override

    if props is None:
        try:
            props = bpy.context.scene.texture_tools_props
        except Exception:
            props = None

    type_format = format_for_texture_type(texture_type, props)
    if type_format:
        return type_format

    dds_format = resolve_dds_target(
        os.path.basename(str(source_path or "")), props, name_hints=name_hints
    )[1]
    return str(dds_format or "bc7_unorm")


def _read_dds_header(path: str):
    """读 DDS 文件前 148 字节；不存在 / 不是 DDS / 头太短都返回 None。"""
    try:
        with open(path, "rb") as handle:
            header = handle.read(_DDS_FULL_HEADER_SIZE)
    except OSError:
        return None
    if len(header) < 128 or header[:4] != b"DDS ":
        return None
    return header


def dds_header_dxgi_format(path: str):
    """DX10 头返回偏移 128 的 DXGI 格式号；legacy FOURCC 头返回 None。"""
    header = _read_dds_header(path)
    if header is None or len(header) < _DDS_FULL_HEADER_SIZE:
        return None
    if header[84:88] != b"DX10":
        return None
    return struct.unpack_from("<I", header, 128)[0]


def existing_dds_matches_format(path: str, dds_format: str) -> bool:
    """判断磁盘上已有的 DDS 是否已经是 ``dds_format`` 指定的格式。

    只读 148 字节文件头，不 import ``common/dds_pixel.py``（那个模块面向完整
    data 解码、且有循环 import 风险）。

    「存在即跳过」会让规则变更（例如 MaterialMap 从 ``bc7_unorm`` 改成
    ``bc7_unorm_srgb``）对旧文件失效，所以复用前必须先验头。任何不确定的
    情况（文件不存在 / 头读不到 / 格式未知）都返回 False —— 重转是安全默认。
    """
    expected = str(dds_format or "").strip().lower()
    if not expected or not os.path.isfile(path):
        return False

    header = _read_dds_header(path)
    if header is None:
        return False

    fourcc = bytes(header[84:88])
    if fourcc == b"DX10":
        if len(header) < _DDS_FULL_HEADER_SIZE:
            return False
        actual = struct.unpack_from("<I", header, 128)[0]
        return _DDS_DXGI_FORMAT_IDS.get(expected) == actual

    # legacy FOURCC 头没有 sRGB 变体：任何 _srgb 目标都判不一致。
    if expected.endswith("_srgb"):
        return False
    legacy_format = _DDS_LEGACY_FOURCC_FORMATS.get(fourcc)
    return legacy_format is not None and legacy_format == expected


def convert_texture_to_dds(
    source_path: str,
    target_path: str,
    props=None,
    texture_type: str = "",
    name_hints=(),
    format_override: str = "",
) -> tuple[bool, str]:
    """把单张贴图转成 DDS 写到 ``target_path``，供 Mod 导出流程复用。

    输出格式由 :func:`resolve_texture_dds_format` 决定：显式 ``format_override``
    优先，否则按「源文件名 → name_hints」推断；色彩空间标志由
    :func:`_texconv_colorspace_flags` 按该格式选择 —— 这正是为「PNG 这类不带
    色彩空间信息的输入」设计的（sRGB 输出用 ``--srgb-in`` 达成恒等，线性输出
    用 ``--ignore-srgb``）。

    **不改动源文件**，返回 ``(True, "")`` 或 ``(False, 失败原因)``。
    """
    texconv_executable = find_texconv()
    if not texconv_executable:
        return False, "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。"

    if props is None:
        props = bpy.context.scene.texture_tools_props

    dds_format = resolve_texture_dds_format(
        source_path,
        props,
        texture_type=texture_type,
        name_hints=name_hints,
        format_override=format_override,
    )

    command = [texconv_executable, "-f", dds_format]
    command.extend(_texconv_colorspace_flags(dds_format))

    # 先转到临时目录再搬过去：texconv 的输出名固定是「源文件名词干 + .dds」，而导出侧
    # 要的目标名通常取材质资源名，两者不同；直接 -o 目标目录会写出一个多余的错误文件名。
    with tempfile.TemporaryDirectory(prefix="herta_dds_export_") as staging:
        command.extend(["-o", staging, "-y", source_path])
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as exc:
            return False, f"调用 texconv 失败: {exc}"

        if process.returncode != 0:
            detail = (process.stderr or process.stdout or "").strip()
            return False, detail or f"texconv 退出码 {process.returncode}"

        produced = os.path.join(
            staging, os.path.splitext(os.path.basename(source_path))[0] + ".dds"
        )
        if not os.path.exists(produced):
            return False, f"texconv 未生成 {produced}"

        target_dir = os.path.dirname(target_path)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        try:
            shutil.move(produced, target_path)
        except Exception as exc:
            return False, f"写出目标文件失败: {exc}"

    return True, ""


class TT_OT_convert_to_dds(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_to_dds"
    bl_label = "批量转换为 .dds"
    bl_description = "使用 texconv.exe 将输出目录中的贴图转换或重编码为目标 DDS 格式，并更新图片引用。按输出格式选择正确色彩空间标志（sRGB 输出用 --srgb-in、线性输出用 --ignore-srgb），保证转换前后数值/颜色不变"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        if not props.output_dir:
            self.report({"ERROR"}, "请先设置输出目录")
            return {"CANCELLED"}

        output_dir_abs = os.path.normpath(bpy.path.abspath(props.output_dir))
        if not os.path.isdir(output_dir_abs):
            self.report({"ERROR"}, f"输出目录不存在: {output_dir_abs}")
            return {"CANCELLED"}

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        texconv_executable = find_texconv()
        if not texconv_executable:
            self.report({"ERROR"}, "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。")
            return {"CANCELLED"}

        supported_extensions = set(_SUPPORTED_SOURCE_EXTENSIONS)
        if props.dds_reencode_existing_dds:
            supported_extensions.add(".dds")

        conversion_map = {}
        converted_files_count = 0
        skipped_files_count = 0
        skipped_unmatched_dds_count = 0

        blend_dir = os.path.normpath(bpy.path.abspath("//"))

        for root, _dirs, files in os.walk(output_dir_abs):
            for filename in files:
                name_no_ext, ext = os.path.splitext(filename)
                ext_lower = ext.lower()
                if ext_lower not in supported_extensions:
                    continue

                old_path = os.path.normpath(os.path.join(root, filename))
                if not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过输出目录外的文件: {filename}")
                    continue

                if old_path.startswith(blend_dir) and not old_path.startswith(output_dir_abs):
                    self.report({"WARNING"}, f"跳过工程目录内的源文件: {filename}")
                    continue

                new_path = os.path.normpath(os.path.join(root, f"{name_no_ext}.dds"))
                texture_type, dds_format, _matched_by = resolve_dds_target(filename, props)

                if ext_lower == ".dds" and texture_type == "default":
                    skipped_unmatched_dds_count += 1
                    continue

                if not dds_format:
                    dds_format = "bc7_unorm"

                command = [texconv_executable, "-f", dds_format]
                command.extend(_texconv_colorspace_flags(dds_format))
                command.extend(["-o", root, "-y", old_path])

                try:
                    process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=True,
                        encoding="utf-8",
                        errors="ignore",
                    )
                except subprocess.CalledProcessError as exc:
                    stderr = (exc.stderr or exc.stdout or str(exc)).strip()
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {stderr}")
                    continue
                except Exception as exc:
                    self.report({"WARNING"}, f"处理文件 {filename} 时出错: {exc}")
                    continue

                if process.returncode != 0:
                    self.report({"WARNING"}, f"转换文件 {filename} 失败: {process.stderr}")
                    continue

                conversion_map[old_path] = (new_path, dds_format)
                converted_files_count += 1

                if props.dds_delete_originals and old_path != new_path:
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
                else:
                    skipped_files_count += 1 if old_path == new_path else 0

        if converted_files_count == 0:
            self.report({"INFO"}, "在输出目录中未找到可转换的贴图文件。")
            return {"CANCELLED"}

        updated_images_count = 0
        for image in bpy.data.images:
            if image.source != "FILE" or not image.filepath:
                continue
            try:
                abs_filepath = os.path.normpath(bpy.path.abspath(image.filepath_raw))
                if abs_filepath in conversion_map:
                    new_path, dds_format = conversion_map[abs_filepath]
                    # 重连只换文件路径与容器格式：先快照用户既有的显示配置，重载后原样写回
                    display_state = _snapshot_image_display_state(image)
                    try:
                        image.filepath = new_path
                        image.reload()
                    finally:
                        # reload 失败也要写回：路径已经换过了，配置同样不该被改写
                        _restore_image_display_state(image, display_state, dds_format)
                    updated_images_count += 1
            except Exception as exc:
                self.report({"WARNING"}, f"更新图片 '{image.name}' 的路径时出错: {exc}")

        self.report(
            {"INFO"},
            f"成功处理 {converted_files_count} 个贴图文件，更新了 {updated_images_count} 个图片引用。"
            + (" 其中部分 DDS 为原地重编码。" if skipped_files_count else ""),
        )
        if skipped_unmatched_dds_count:
            self.report(
                {"INFO"},
                f"跳过了 {skipped_unmatched_dds_count} 个未命中任何 DDS 规则的现有 DDS 文件。",
            )
        return {"FINISHED"}


class TT_OT_add_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_add_dds_rule"
    bl_label = "添加DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        rule = props.dds_rules.add()
        rule.pattern = ".*"
        rule.format = "bc7_unorm"
        rule.enabled = True
        props.dds_use_custom_rules = True
        return {"FINISHED"}


class TT_OT_remove_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_remove_dds_rule"
    bl_label = "移除DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.remove(self.index)
        return {"FINISHED"}


class TT_OT_reset_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_reset_dds_rules"
    bl_label = "重置DDS规则"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.texture_tools_props
        props.dds_rules.clear()
        props.dds_use_custom_rules = True

        for rule_data in DDS_DEFAULT_RULES:
            rule = props.dds_rules.add()
            rule.pattern = rule_data["pattern"]
            rule.format = rule_data["format"]
            rule.enabled = True

        return {"FINISHED"}


class TT_OT_test_dds_rule(bpy.types.Operator):
    bl_idname = "toolkit.tt_test_dds_rule"
    bl_label = "测试DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        rule_errors = _validate_custom_rules(props)
        if rule_errors:
            self.report({"ERROR"}, "；".join(rule_errors))
            return {"CANCELLED"}

        test_names = [
            "DiffuseMap_Body.png",
            "Body-DiffuseMap.dds",
            "NormalMap_Face.dds",
            "LightMap_Hair.tga",
            "MaterialMap_Armor.png",
            "Body-ORMMap.png",
            "RampMap_Eye.png",
            "HighLightMap_Hair.png",
            "WengineFX_Leg.bmp",
            "Glowmap_1_Eye.png",
            "FXMap_Body.dds",
            "RoughnessMap_Body.png",
            "UnknownMask.png",
            "VivianGemADiffuse.png",
        ]

        result_lines = ["DDS规则测试结果:"]
        for name in test_names:
            texture_type, matched_format, matched_by = resolve_dds_target(name, props)
            result_lines.append(f"  {name} -> {matched_format} ({texture_type} / {matched_by})")

        self.report({"INFO"}, "\n".join(result_lines))
        return {"FINISHED"}


class TT_OT_save_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_save_dds_rules"
    bl_label = "保存DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        rules_data = []
        for rule in props.dds_rules:
            rules_data.append(
                {
                    "pattern": rule.pattern,
                    "format": rule.format,
                    "enabled": rule.enabled,
                }
            )

        try:
            with open(props.dds_rules_file_path, "w", encoding="utf-8") as file_obj:
                json.dump(rules_data, file_obj, indent=2, ensure_ascii=False)
            self.report({"INFO"}, f"规则已保存到: {props.dds_rules_file_path}")
        except Exception as exc:
            self.report({"ERROR"}, f"保存失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


class TT_OT_load_dds_rules(bpy.types.Operator):
    bl_idname = "toolkit.tt_load_dds_rules"
    bl_label = "加载DDS规则"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.texture_tools_props

        if not props.dds_rules_file_path:
            self.report({"ERROR"}, "请先指定规则文件路径")
            return {"CANCELLED"}

        if not os.path.exists(props.dds_rules_file_path):
            self.report({"ERROR"}, "规则文件不存在")
            return {"CANCELLED"}

        try:
            with open(props.dds_rules_file_path, "r", encoding="utf-8") as file_obj:
                rules_data = json.load(file_obj)

            props.dds_rules.clear()
            for rule_data in rules_data:
                rule = props.dds_rules.add()
                rule.pattern = rule_data.get("pattern", ".*")
                rule.format = rule_data.get("format", "bc7_unorm")
                rule.enabled = rule_data.get("enabled", True)
            props.dds_use_custom_rules = True

            self.report({"INFO"}, f"已加载 {len(rules_data)} 条规则")
        except Exception as exc:
            self.report({"ERROR"}, f"加载失败: {exc}")
            return {"CANCELLED"}

        return {"FINISHED"}


tt_dds_conversion_list = (
    TT_OT_convert_to_dds,
    TT_OT_add_dds_rule,
    TT_OT_remove_dds_rule,
    TT_OT_reset_dds_rules,
    TT_OT_test_dds_rule,
    TT_OT_save_dds_rules,
    TT_OT_load_dds_rules,
)
