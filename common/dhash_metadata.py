"""dHash 贴图元数据的纯函数契约（无 bpy / 无 GlobalProterties 依赖）。

导出侧（ui/universal/efmi.py）与材质侧后处理共用这里的规范化与命名，
避免两边各拼一套字符串导致 filter 段名对不上。
"""

import re


DHASH_RE = re.compile(r"^[0-9a-f]{16}$")

_FORMAT_RE = re.compile(r"^[0-9A-Z_]+$")
_TOKEN_INVALID_RE = re.compile(r"[^A-Za-z0-9_]+")
_DXGI_FORMAT_PREFIX = "DXGI_FORMAT_"

_DHASH_FILTER_PREFIX = "Filter_"
_DEFAULT_TOKEN = "Submesh"
_DEFAULT_ROLE = "Tex"

# mark_name -> filter 角色。大小写不敏感；键用 lower 形式。
_DHASH_ROLE_MAP = {
    "diffusemap": "Diffuse",
    "diffuse": "Diffuse",
    "lightmap": "Light",
    "light": "Light",
    "normalmap": "Normal",
    "normal": "Normal",
    "materialmap": "Material",
    "material": "Material",
}


def normalize_dhash(value) -> str:
    """strip + lower；只接受 16 位小写十六进制，其它一律返回空串。"""
    text = str(value if value is not None else "").strip().lower()
    return text if DHASH_RE.fullmatch(text) else ""


def normalize_format(value) -> str:
    """strip + upper；去掉可选的 DXGI_FORMAT_ 前缀；非法/空返回空串。"""
    text = str(value if value is not None else "").strip().upper()
    if text.startswith(_DXGI_FORMAT_PREFIX):
        text = text[len(_DXGI_FORMAT_PREFIX):]
    if not text or not _FORMAT_RE.fullmatch(text):
        return ""
    return text


def normalize_binding_mode(value) -> str:
    """filter/dhash（大小写不敏感）→ "filter"；其余（含缺省）→ "slot"。"""
    text = str(value if value is not None else "").strip().lower()
    if text in ("filter", "dhash"):
        return "filter"
    return "slot"


def _first_mark_value(mark, names):
    """按顺序读标记对象的第一个非空字段；兼容 dict 与属性两种载体。"""
    for name in names:
        try:
            if isinstance(mark, dict):
                if name not in mark:
                    continue
                value = mark[name]
            else:
                value = getattr(mark, name, None)
        except Exception:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return ""


def get_mark_dhash(mark):
    return _first_mark_value(mark, ("source_dhash", "dhash", "SourceDHash"))


def get_mark_format(mark):
    return _first_mark_value(mark, ("source_format", "format", "SourceFormat"))


def get_mark_binding_mode(mark):
    return _first_mark_value(mark, ("binding_mode", "BindingMode"))


def _sanitize_token(value, fallback: str) -> str:
    text = _TOKEN_INVALID_RE.sub("_", str(value if value is not None else "").strip())
    text = text.strip("_")
    return text or fallback


def role_for_mark_name(mark_name) -> str:
    """贴图标记名 -> filter 角色（导出侧与材质侧共用同一张映射表）。

    DiffuseMap/Diffuse、LightMap/Light、NormalMap/Normal、MaterialMap/Material
    大小写不敏感；其它名字清洗到 `[A-Za-z0-9_]`，空/全非法时兜底 `Tex`。
    """
    name = str(mark_name if mark_name is not None else "").strip()
    mapped = _DHASH_ROLE_MAP.get(name.lower())
    if mapped is not None:
        return mapped
    return _sanitize_token(name, _DEFAULT_ROLE)


def _filter_name(token: str, role: str, suffix: str = "") -> str:
    parts = [
        _sanitize_token(token, _DEFAULT_TOKEN),
        _sanitize_token(role, _DEFAULT_ROLE),
    ]
    clean_suffix = _sanitize_token(suffix, "")
    if clean_suffix:
        parts.append(clean_suffix)
    return _DHASH_FILTER_PREFIX + "_".join(parts)


def filter_name_for(token: str, role: str) -> str:
    """唯一拼接点：`Filter_<清理后的 token>_<清理后的 role>`。

    token/role 中不属于 `[A-Za-z0-9_]` 的字符一律替换成 `_`。
    """
    return _filter_name(token, role)


def filter_name_with_suffix(token: str, role: str, suffix: str) -> str:
    """同一 (token, role) 下出现第二张不同原图时的消歧名。

    仍在 `common/dhash_metadata.py` 内拼接，导出侧只传 dhash 前缀做后缀。
    """
    return _filter_name(token, role, suffix)


def filter_section_lines(fmt: str, dhash: str) -> list[str]:
    """filter 段体两行；format 去 `DXGI_FORMAT_` 前缀，dhash 转 16 位小写。"""
    return [
        "match_format = " + normalize_format(fmt),
        "match_dhash = " + normalize_dhash(dhash),
    ]
